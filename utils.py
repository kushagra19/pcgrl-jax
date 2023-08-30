import os

import gymnax
import jax

from config import Config
from envs.binary_0 import Binary0
from envs.candy import Candy, CandyParams
from envs.pcgrl_env import PCGRLEnvParams, PCGRLEnv
from models import ActorCritic, AutoEncoder, ConvForward, Dense, NCA, SeqNCA


def get_exp_dir(config: Config):
    if config.env_name == 'PCGRL':
        exp_dir = os.path.join(
            'saves',
            f'{config.problem}_{config.representation}_{config.model}-' +
            f'{config.activation}_w-{config.map_width}_rf-{config.arf_size}_' +
            f'arf-{config.arf_size}_sp-{config.static_tile_prob}_' + \
            f'fz-{config.n_freezies}_' + \
            f'act-{config.act_shape[0]}x{config.act_shape[1]}_' + \
            f'nag-{config.n_agents}_' + \
            f'{config.seed}_{config.exp_name}')
    elif config.env_name == 'Candy':
        exp_dir = os.path.join(
            'saves',
            'candy_' + \
            f'{config.seed}_{config.exp_name}',
        )
    return exp_dir


def init_config(config: Config):
    config.n_gpus = jax.local_device_count()

    if config.env_name == 'Candy':
        config.exp_dir = get_exp_dir(config)
        return config

    config.arf_size = (2 * config.map_width -
                      1 if config.arf_size is None else config.arf_size)
    config.arf_size = config.arf_size if config.arf_size is None \
        else config.arf_size
    config.exp_dir = get_exp_dir(config)
    return config


def get_ckpt_dir(config: Config):
    return os.path.join(get_exp_dir(config), 'ckpts')


def get_network(env: PCGRLEnv, env_params: PCGRLEnvParams, config: Config):
    if config.env_name == 'Candy':
        # In the candy-player environment, action space is flat discrete space over all candy-direction combos.
        action_dim = env.action_space(env_params).n

    else:
        # First consider number of possible tiles
        # action_dim = env.action_space(env_params).n
        action_dim = len(env.tile_enum) - 1
        if config.representation == "wide":
            action_dim = len(env.tile_enum) - 1
        action_dim = action_dim * config.n_agents

    if config.model == "dense":
        network = Dense(
            action_dim, activation=config.activation,
            arf_size=config.arf_size, vrf_size=config.vrf_size,
        )
    if config.model == "conv":
        network = ConvForward(
            action_dim=action_dim, activation=config.activation,
            arf_size=config.arf_size, act_shape=config.act_shape,
            vrf_size=config.vrf_size,
        )
    if config.model == "seqnca":
        network = SeqNCA(
            action_dim, activation=config.activation,
            arf_size=config.arf_size,
            vrf_size=config.vrf_size,
        )
    if config.model in {"nca", "autoencoder"}:
        if config.model == "nca":
            network = NCA(
                representation=config.representation,
                action_dim=action_dim,
                activation=config.activation,
            )
        elif config.model == "autoencoder":
            network = AutoEncoder(
                representation=config.representation,
                action_dim=action_dim,
                activation=config.activation,
            )
    network = ActorCritic(network, act_shape=config.act_shape,
                          n_agents=config.n_agents)
    return network


def get_env_params_from_config(config: Config):
    map_shape = (config.map_width, config.map_width)
    rf_size = max(config.arf_size, config.vrf_size)
    rf_shape = (rf_size, rf_size)
    env_params = PCGRLEnvParams(
        map_shape=map_shape,
        rf_shape=rf_shape,
        act_shape=tuple(config.act_shape),
        static_tile_prob=config.static_tile_prob,
        n_freezies=config.n_freezies,
        n_agents=config.n_agents,
        max_board_scans=config.max_board_scans,
    )
    return env_params


def gymnax_pcgrl_make(env_name, config: Config, **env_kwargs):
    if env_name in gymnax.registered_envs:
        return gymnax.make(env_name)

    elif env_name == 'PCGRL':
        env_params = get_env_params_from_config(config)
        env = PCGRLEnv(env_params)

    elif env_name == 'Binary0':
        env = Binary0(**env_kwargs)

    elif env_name == 'Candy':
        env_params = CandyParams()
        env = Candy(env_params)

    return env, env_params