from enum import IntEnum
from functools import partial
from typing import Optional, Tuple

import chex
from flax import struct
import jax
import jax.numpy as jnp
import numpy as np

from gymnax.environments.environment import Environment
from envs.pathfinding import get_path_coords
from envs.probs.binary import BinaryProblem
from envs.probs.problem import Problem, ProblemState
from envs.reps.narrow import NarrowRepresentation
from envs.reps.turtle import MultiTurtleRepresentation, TurtleRepresentation
from envs.reps.wide import WideRepresentation
from envs.reps.nca import NCARepresentation
from envs.reps.representation import Representation, RepresentationState
from envs.utils import Tiles
from sawtooth import triangle_wave


class ProbEnum(IntEnum):
    BINARY = 0
    DUNEGON = 1


class RepEnum(IntEnum):
    NARROW = 0
    TURTLE = 1
    WIDE = 2
    NCA = 3

@struct.dataclass
class PCGRLEnvState:
    env_map: chex.Array
    static_map: chex.Array
    rep_state: RepresentationState
    prob_state: Optional[ProblemState] = None
    step_idx: int = 0
    reward: np.float32 = 0.0
    done: bool = False


@struct.dataclass
class PCGRLEnvParams:
    problem: ProbEnum = ProbEnum.BINARY
    representation: RepEnum = RepEnum.NARROW
    map_shape: Tuple[int, int] = (16, 16)
    act_shape: Tuple[int, int] = (1, 1)
    rf_shape: Tuple[int, int] = (31, 31)
    static_tile_prob: Optional[float] = 0.0
    n_freezies: int = 0
    n_agents: int = 1
    max_board_scans: float = 3.0


def gen_init_map(rng, tile_enum, map_shape, tile_probs):
    init_map = jax.random.choice(
        rng, len(tile_enum), shape=map_shape, p=tile_probs)
    return init_map


def gen_static_tiles(rng, static_tile_prob, n_freezies, map_shape):
    static_rng, rng = jax.random.split(rng)
    static_tiles = jax.random.bernoulli(
        static_rng, p=static_tile_prob, shape=map_shape)
    if n_freezies > 0:
        def gen_freezie(rng):
            # Randomly select row or column 
            rng, rng_ = jax.random.split(rng)

            height = map_shape[0]
            width = map_shape[1]
            
            rect = jnp.ones(map_shape, dtype=jnp.float16)
            row = rect[0]
            col = rect[1]

            locs = jax.random.uniform(rng_, shape=(2,))
            r_loc, c_loc = locs

            r_tri = triangle_wave(jnp.arange(map_shape[1]) / map_shape[1], 
                                   x_peak=r_loc, period=2)
            c_tri = triangle_wave(jnp.arange(map_shape[0]) / map_shape[0], 
                                   x_peak=c_loc, period=2)
            rc_tris = jnp.stack((r_tri, c_tri))
            maxval = jnp.max(rc_tris)
            minval = jnp.min(rc_tris)
            rc_cutoff = jax.random.uniform(rng_, shape=(2,), minval=minval*1.5, maxval=maxval)
            r_cut, c_cut = rc_cutoff
            r_tri = jnp.where(r_tri > r_cut, 1, 0)
            c_tri = jnp.where(c_tri > c_cut, 1, 0)

            rect = rect * r_tri * c_tri[..., None]
            rect = rect.astype(bool)

            return rect

        frz_keys = jax.random.split(rng, n_freezies)

        rects = jax.vmap(gen_freezie, in_axes=0)(frz_keys)

        rects = jnp.clip(rects.sum(0), 0, 1).astype(bool)
        static_tiles = rects | static_tiles

        # frz_xy = jax.random.randint(rng, shape=(n_freezies, 2), minval=0, maxval=map_shape)
        # frz_len = jax.random.randint(rng, shape=(n_freezies, 2), minval=1,
        #                                 # maxval=(map_shape[0] - frz_xy[:, 0], map_shape[1] - frz_xy[:, 1]))
        #                                 maxval=map_shape)
        # frz_len_1 = jnp.ones((n_freezies, 2), dtype=jnp.int32)
        # # frz_len_1 = np.ones((n_freezies, 2), dtype=jnp.int32)
        # frz_dirs = jax.random.randint(rng, shape=(n_freezies,), minval=0, maxval=2)
        # # frz_dirs = np.array(frz_dirs)
        # frz_len = frz_len_1.at[frz_dirs].set(frz_len[frz_dirs])
        # # frz_len[frz_dirs] = frz_len_1[frz_dirs]
        # for xy, len in zip(frz_xy, frz_len):
        #     static_tiles = jax.lax.dynamic_update_slice(static_tiles, jnp.ones(len), xy)
    return static_tiles


class PCGRLEnv(Environment):
    def __init__(self, env_params: PCGRLEnvParams):
        map_shape, act_shape, rf_shape, problem, representation, static_tile_prob, n_freezies, n_agents = \
            env_params.map_shape, env_params.act_shape, env_params.rf_shape, env_params.problem, env_params.representation, env_params.static_tile_prob, env_params.n_freezies, env_params.n_agents

        self.map_shape = map_shape
        self.act_shape = act_shape
        self.static_tile_prob = np.float32(static_tile_prob)
        self.n_freezies = np.int32(n_freezies)
        self.n_agents = n_agents

        self.prob: Problem
        if problem == ProbEnum.BINARY:
            self.prob = BinaryProblem(map_shape=self.map_shape)
        else:
            raise Exception(f'Problem {problem} not implemented')

        self.tile_enum = self.prob.tile_enum
        self.tile_probs = self.prob.tile_probs
        rng = jax.random.PRNGKey(0)  # Dummy random key
        env_map = gen_init_map(rng, self.tile_enum, self.map_shape,
                               self.tile_probs)

        self.rep: Representation
        if representation == RepEnum.NARROW:
            self.rep = NarrowRepresentation(env_map=env_map, rf_shape=rf_shape,
                                            tile_enum=self.tile_enum,
                                            act_shape=act_shape,
                                            max_board_scans=env_params.max_board_scans,
            )
        elif representation == RepEnum.NCA:
            self.rep = NCARepresentation(env_map=env_map, rf_shape=rf_shape,
                                         tile_enum=self.tile_enum,
                                         act_shape=act_shape,
                                         max_board_scans=env_params.max_board_scans,
            )
        elif representation == RepEnum.WIDE:
            self.rep = WideRepresentation(env_map=env_map, rf_shape=rf_shape,
                                          tile_enum=self.tile_enum,
                                          act_shape=act_shape,
                                          max_board_scans=env_params.max_board_scans,
            )
        elif representation == RepEnum.TURTLE:
            if n_agents > 1:
                self.rep = MultiTurtleRepresentation(
                    env_map=env_map, rf_shape=rf_shape,
                    tile_enum=self.tile_enum,
                    act_shape=act_shape,
                    map_shape=map_shape,
                    n_agents=n_agents,
                    max_board_scans=env_params.max_board_scans,
                    )

            else:
                self.rep = TurtleRepresentation(env_map=env_map, rf_shape=rf_shape,
                                                tile_enum=self.tile_enum,
                                                act_shape=act_shape, map_shape=map_shape)
        else:
            raise Exception(f'Representation {representation} not implemented')

        self.max_steps = self.rep.max_steps
        self.tile_size = self.prob.tile_size

    def init_graphics(self):
        self.prob.init_graphics()

    @partial(jax.jit, static_argnums=(0, 2))
    def reset_env(self, rng, env_params: PCGRLEnvParams) \
            -> Tuple[chex.Array, PCGRLEnvState]:
        env_map = gen_init_map(rng, self.tile_enum,
                               self.map_shape, self.tile_probs)
        if self.static_tile_prob is not None or self.n_freezies > 0:
            frz_map = gen_static_tiles(
                rng, self.static_tile_prob, self.n_freezies, self.map_shape)
        else:
            frz_map = None

        rep_state = self.rep.reset(frz_map, rng)
        obs = self.rep.get_obs(
            env_map=env_map, static_map=frz_map, rep_state=rep_state)

        _, prob_state = self.prob.get_stats(env_map)
        rep_state = self.rep.reset(frz_map, rng=rng)
        env_state = PCGRLEnvState(env_map=env_map, static_map=frz_map,
                                  rep_state=rep_state, prob_state=prob_state,
                                  step_idx=0, done=False)

        return obs, env_state

    @partial(jax.jit, static_argnums=(0, 4))
    def step_env(self, rng, env_state: PCGRLEnvState, action, env_params):
        if self.n_agents == 1:
            action = action[0]
        env_map, map_changed, rep_state = self.rep.step(
            env_map=env_state.env_map, action=action,
            rep_state=env_state.rep_state, step_idx=env_state.step_idx
        )
        env_map = jnp.where(env_state.static_map == 1,
                            env_state.env_map, env_map
                            )
        reward, prob_state = jax.lax.cond(
            map_changed,
            lambda env_map: self.prob.get_stats(env_map, env_state.prob_state),
            lambda _: (0., env_state.prob_state),
            env_map,
        )
        obs = self.rep.get_obs(
            env_map=env_map, static_map=env_state.static_map,
            rep_state=rep_state)
        done = self.is_terminal(env_state, env_params)
        step_idx = env_state.step_idx + 1
        env_state = PCGRLEnvState(
            env_map=env_map, static_map=env_state.static_map,
            rep_state=rep_state, done=done, reward=reward,
            prob_state=prob_state, step_idx=step_idx)

        return (
            jax.lax.stop_gradient(obs),
            jax.lax.stop_gradient(env_state),
            reward,
            done,
            {"discount": self.discount(env_state, env_params)},
        )

    def is_terminal(self, state: PCGRLEnvState, params: PCGRLEnvParams) \
            -> bool:
        """Check whether state is terminal."""
        done_steps = state.step_idx >= (self.rep.max_steps - 1)
        return done_steps

    def render(self, env_state: PCGRLEnvState):
        # TODO: Refactor this into problem
        path_coords = get_path_coords(
            env_state.prob_state.flood_count,
            self.prob.max_path_len)
        return render_map(self, env_state, path_coords)

    @property
    def default_params(self) -> PCGRLEnvParams:
        return PCGRLEnvParams(map_shape=(16, 16))

    def action_space(self, env_params: PCGRLEnvParams) -> int:
        return self.rep.action_space()

    def observation_space(self, env_params: PCGRLEnvParams) -> int:
        return self.rep.observation_space()

    def action_shape(self):
        return (self.n_agents, *self.act_shape, len(self.tile_enum) - 1)

    def sample_action(self, rng):
        action_shape = self.action_shape()
        # Sample an action from the action space
        n_dims = len(action_shape)
        act_window_shape = action_shape[:-1]
        n_tiles = action_shape[-1]
        return jax.random.randint(rng, act_window_shape, 0, n_tiles)[None, ...]


def render_map(env: PCGRLEnv, env_state: PCGRLEnvState,
               path_coords: chex.Array):
    tile_size = env.prob.tile_size
    env_map = env_state.env_map
    border_size = np.array((1, 1))
    env_map = jnp.pad(env_map, border_size, constant_values=Tiles.BORDER)
    full_height = len(env_map)
    full_width = len(env_map[0])
    lvl_img = jnp.zeros(
        (full_height*tile_size, full_width*tile_size, 4), dtype=jnp.uint8)
    lvl_img = lvl_img.at[:].set((0, 0, 0, 255))

    # Map tiles
    for y in range(len(env_map)):
        for x in range(len(env_map[y])):
            tile_img = env.prob.graphics[env_map[y][x]]
            lvl_img = lvl_img.at[y*tile_size: (y+1)*tile_size,
                                 x*tile_size: (x+1)*tile_size, :].set(tile_img)

    # Path, if applicable
    tile_img = env.prob.graphics[-1]

    def draw_path_tile(carry):
        path_coords, lvl_img, i = carry
        y, x = path_coords[i]
        lvl_img = jax.lax.dynamic_update_slice(lvl_img, tile_img,
                                               ((y + border_size[0]) * tile_size, (x + border_size[1]) * tile_size, 0))
        return (path_coords, lvl_img, i+1)

    def cond(carry):
        path_coords, _, i = carry
        return jnp.all(path_coords[i] != jnp.array((-1, -1)))

    i = 0
    _, lvl_img, _ = jax.lax.while_loop(
        cond, draw_path_tile, (path_coords, lvl_img, i))

    clr = (255, 255, 255, 255)
    y_border = jnp.zeros((2, tile_size, 4), dtype=jnp.uint8)
    y_border = y_border.at[:, :, :].set(clr)
    x_border = jnp.zeros((tile_size, 2, 4), dtype=jnp.uint8)
    x_border = x_border.at[:, :, :].set(clr)
    if hasattr(env_state.rep_state, 'pos'):

        def render_pos(a_pos, lvl_img):
            y, x = a_pos
            y, x = y + border_size[0], x + border_size[1]
            y, x = y * tile_size, x * tile_size
            lvl_img = jax.lax.dynamic_update_slice(lvl_img, x_border, (y, x, 0))
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, x_border, (y, x+tile_size-2, 0))
            lvl_img = jax.lax.dynamic_update_slice(lvl_img, y_border, (y, x, 0))
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, y_border, (y+tile_size-2, x, 0))
            return lvl_img

        if env_state.rep_state.pos.ndim == 1:
            a_pos = env_state.rep_state.pos
            lvl_img = render_pos(a_pos, lvl_img)
        elif env_state.rep_state.pos.ndim == 2:
            for a_pos in env_state.rep_state.pos:
                lvl_img = render_pos(a_pos, lvl_img)

    clr = (255, 0, 0, 255)
    x_border = x_border.at[:, :, :].set(clr)
    y_border = y_border.at[:, :, :].set(clr)
    if env.static_tile_prob is not None or env.n_freezies > 0:
        static_map = env_state.static_map
        static_coords = jnp.argwhere(static_map,
                                     size=(
                                         env_map.shape[0]-border_size[0])*(env_map.shape[1]-border_size[1]),
                                     fill_value=-1)

        def draw_static_tile(carry):
            static_coords, lvl_img, i = carry
            y, x = static_coords[i]
            y, x = y + border_size[1], x + border_size[0]
            y, x = y * tile_size, x * tile_size
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, x_border, (y, x, 0))
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, x_border, (y, x+tile_size-2, 0))
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, y_border, (y, x, 0))
            lvl_img = jax.lax.dynamic_update_slice(
                lvl_img, y_border, (y+tile_size-2, x, 0))
            return (static_coords, lvl_img, i+1)

        def cond(carry):
            static_coords, _, i = carry
            return jnp.all(static_coords[i] != jnp.array((-1, -1)))

        i = 0
        _, lvl_img, _ = jax.lax.while_loop(
            cond, draw_static_tile, (static_coords, lvl_img, i))

    return lvl_img