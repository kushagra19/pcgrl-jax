import json
import os

import chex
from flax import struct
import hydra
import imageio
import jax
import jax.numpy as jnp
from matplotlib import pyplot as plt
import numpy as np

from config import EvalConfig
from envs.probs.problem import ProblemState
from train import gen_dummy_queued_state, init_checkpointer
from utils import get_exp_dir, get_network, gymnax_pcgrl_make, init_config


@struct.dataclass
class EvalData:
    cell_losses: chex.Array
    cell_progs: chex.Array
    cell_rewards: chex.Array

@hydra.main(version_base=None, config_path='./', config_name='eval_pcgrl')
def main_eval_cp(config: EvalConfig):
    config = init_config(config, evo=False)

    exp_dir = get_exp_dir(config)
    if not config.random_agent:
        checkpoint_manager, restored_ckpt = init_checkpointer(config)
        network_params = restored_ckpt['runner_state'].train_state.params
    elif not os.path.exists(exp_dir):
        os.makedirs(exp_dir)

    env, env_params = gymnax_pcgrl_make(config.env_name, config=config)
    env.prob.init_graphics()
    network = get_network(env, env_params, config)

    rng = jax.random.PRNGKey(42)
    reset_rng = jax.random.split(rng, config.n_envs)

    def eval_cp(change_pct, env_params):
        # obs, env_state = env.reset(reset_rng, env_params)
        queued_state = gen_dummy_queued_state(env)
        env_params = env_params.replace(change_pct=change_pct)
        obs, env_state = jax.vmap(env.reset, in_axes=(0, None, None))(
            reset_rng, env_params, queued_state)

        def step_env(carry, _):
            rng, obs, env_state = carry
            rng, rng_act = jax.random.split(rng)
            if config.random_agent:
                action = env.action_space(env_params).sample(rng_act)
            else:
                # obs = jax.tree_map(lambda x: x[None, ...], obs)
                action = network.apply(network_params, obs)[0].sample(seed=rng_act)

            rng_step = jax.random.split(rng, config.n_envs)
            obs, env_state, reward, done, info = jax.vmap(env.step, in_axes=(0, 0, 0, None))(
                rng_step, env_state, action, env_params
            )
            # frame = env.render(env_state)
            # Can't concretize these values inside jitted function (?)
            # So we add the stats on cpu later (below)
            # frame = render_stats(env, env_state, frame)
            return (rng, obs, env_state), (env_state, reward, done, info)

        print('Scanning episode steps:')
        _, (states, rewards, dones, infos) = jax.lax.scan(
            step_env, (rng, obs, env_state), None,
            length=config.n_eps*env.max_steps)

        return _, (states, rewards, dones, infos)

    def _eval_cp(change_pct, env_params):
        _, (states, reward, dones, infos) = eval_cp(change_pct, env_params)
        ep_rewards = reward.sum(axis=0)
        cell_reward = jnp.mean(ep_rewards)

        cell_states = states.prob_state
        init_prob_state: ProblemState = cell_states[0]
        final_prob_state: ProblemState = cell_states[-1]

        # Compute weighted loss from targets
        cell_loss = jnp.mean(jnp.abs(
            final_prob_state.ctrl_trgs - final_prob_state.stats) * env.prob.ct
        )

        # Compute relative progress toward target from initial metric values
        # cell_progs = 1 - jnp.abs(final_stats[:, ctrl_metrics] - ctrl_trg) / jnp.abs(init_stats[:, ctrl_metrics] - ctrl_trg)
        final_prog = jnp.abs(final_prob_state.stats - final_prob_state.ctrl_trgs)
        trg_prog = jnp.abs(init_prob_state.stats - init_prob_state.ctrl_trgs)
        trg_prog = trg_prog if trg_prog != 0 else 1e4
        cell_progs = (1 - jnp.abs(final_prog / trg_prog))
        cell_prog = jnp.mean(cell_progs)

        eval_data = EvalData(
            cell_losses=cell_loss,
            cell_rewards=cell_reward,
            cell_progs = cell_prog,
        )
        
        return eval_data

    json_path = os.path.join(exp_dir, 'cp_stats.json')

    # For each bin, evaluate the change pct. at the center of the bin
    change_pcts = np.linspace(
        config.n_bins,0,1
    )
    
    if config.reevaluate:
        stats = jax.vmap(_eval_cp, in_axes=(0, None))(change_pcts, env_params)

        with open(json_path, 'w') as f:
            json_stats = {k: v.tolist() for k, v in stats.__dict__.items()}
            json.dump(json_stats, f, indent=4)
    else:
        with open(json_path, 'r') as f:
            stats = json.load(f)
            stats = EvalData(**stats)

    # Make a bar plot of cell losses
    fig, ax = plt.subplots()
    ax.bar(np.arange(len(stats.cell_losses)), stats.cell_losses)
    ax.set_xticks(np.arange(len(stats.cell_losses)))
    ax.set_xticklabels(change_pcts)
    ax.set_ylabel('Loss')
    ax.set_xlabel('Change pct.')

    # cell_progs = np.array(stats.cell_progs)

    # fig, ax = plt.subplots()
    # ax.imshow(cell_progs)
    # if len(im_shape) == 1:
    #     ax.set_xticks([])
    # elif len(im_shape) == 2:
    #     ax.set_xticks(np.arange(len(ctrl_trgs[0])))
    #     ax.set_xticklabels(ctrl_trgs[0])
    # ax.set_yticks(np.arange(len(ctrl_trgs))) 
    # ax.set_yticklabels(ctrl_trgs[:, 0])
    # fig.colorbar()
    # plt.imshow(cell_progs)
    # plt.colorbar()
    # plt.title('Control target success')

    plt.savefig(os.path.join(exp_dir, 'ctrl_loss.png'))

if __name__ == '__main__':
    main_eval_cp()