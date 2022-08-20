import taichi as ti

import utils
from utils import *
from mgpcg import MGPCGPoissonSolver
from pressure_project import PressureProjectStrategy
from level_set import FastSweepingLevelSet
from volume_control import PressureProjectWithVolumeControlStrategy

from functools import reduce
import time
import numpy as np

ti.init(arch=ti.cuda, kernel_profiler=False, device_memory_GB=4.0)

ADVECT_REDISTANCE = 0
MARKERS = 1

FAST_SWEEPING_METHOD = 0
FAST_MARCHING_METHOD = 1

@ti.data_oriented
class FluidSimulator:
    def __init__(self,
        dim = 2,
        res = (128, 128),
        dt = 1.25e-2,
        substeps = 1,
        dx = 1.0,
        rho = 1000.0,
        gravity = [0, -9.8],
        p0 = 1e-3,
        real = float):

        # ADVECT_REDISTANCE: advect the level-set with Semi-Lagrangian, then redistance it (Standard)
        # MARKERS: advect markers with Semi-Lagrangian, then build the level-set from markers
        self.solver_type = ADVECT_REDISTANCE

        self.dim = dim
        self.real = real
        self.res = res
        self.dx = dx
        self.dt = dt

        self.total_t = 0.0 # total simulation time

        self.p0 = p0 # the standard atmospheric pressure
        self.rho = rho # density
        self.gravity = gravity # body force
        self.substeps = substeps

        # cell_type
        self.cell_type = ti.field(dtype=ti.i32)

        self.velocity = [ti.field(dtype=real) for _ in range(self.dim)] # MAC grid
        self.velocity_backup = [ti.field(dtype=real) for _ in range(self.dim)] # backup / use as weight in apic update
        self.pressure = ti.field(dtype=real)

        # extrap utils
        self.valid = ti.field(dtype=ti.i32)
        self.valid_temp = ti.field(dtype=ti.i32)

        # marker/apic particles
        self.total_mk = ti.field(dtype=ti.i32, shape = ()) # total number of particles/markers
        self.p_x = ti.Vector.field(dim, dtype=real) # positions
        
        self.indices = ti.ijk if self.dim == 3 else ti.ij
        self.p_per_axis = 2
        self.ppc = self.p_per_axis ** dim
        self.max_particles = reduce(lambda x, y : x * y, res) * (4 ** dim)
        ti.root.dense(ti.i, self.max_particles).place(self.p_x)

        ti.root.dense(self.indices, res).place(self.cell_type, self.pressure)
        ti.root.dense(self.indices, [res[_] + 1 for _ in range(self.dim)]).place(self.valid, self.valid_temp)
        for d in range(self.dim):
            ti.root.dense(self.indices, [res[_] + (d == _) for _ in range(self.dim)]).place(self.velocity[d], self.velocity_backup[d])
        
        # Level-Set
        self.level_set = FastSweepingLevelSet(self.dim, 
                                self.res, 
                                self.dx, 
                                self.real)

        # MGPCG
        self.n_mg_levels = 4
        self.pre_and_post_smoothing = 2
        self.bottom_smoothing = 10
        self.iterations = 50
        self.verbose = False
        self.poisson_solver = MGPCGPoissonSolver(self.dim, 
                                                 self.res, 
                                                 self.n_mg_levels,
                                                 self.pre_and_post_smoothing,
                                                 self.bottom_smoothing,
                                                 self.real)

        # Pressure Solve
        self.ghost_fluid_method = False # Gibou et al. [GFCK02]
        self.volume_control = False
        if self.volume_control:
            self.strategy = PressureProjectWithVolumeControlStrategy(self.dim,
                                                                     self.velocity,
                                                                     self.ghost_fluid_method, 
                                                                     self.level_set.phi, 
                                                                     self.p0,
                                                                     self.level_set,
                                                                     self.dt) # [Losasso et al. 2008]
        else:
            self.strategy = PressureProjectStrategy(self.dim,
                                                    self.velocity,
                                                    self.ghost_fluid_method, 
                                                    self.level_set.phi, 
                                                    self.p0)

    @ti.func
    def is_valid(self, I):
        return all(I >= 0) and all(I < self.res)

    @ti.func
    def is_fluid(self, I):
        return self.is_valid(I) and self.cell_type[I] == utils.FLUID

    @ti.func
    def is_solid(self, I):
        return not self.is_valid(I) or self.cell_type[I] == utils.SOLID

    @ti.func
    def is_air(self, I):
        return self.is_valid(I) and self.cell_type[I] == utils.AIR

    @ti.func
    def vel_interp(self, pos):
        v = ti.Vector.zero(self.real, self.dim)
        for k in ti.static(range(self.dim)):
            v[k] = utils.sample(self.velocity[k], pos / self.dx - 0.5 * (1 - ti.Vector.unit(self.dim, k)))
        return v

    @ti.kernel
    def advect_markers(self, dt : ti.f32):
        for p in range(self.total_mk[None]):
            midpos = self.p_x[p] + self.vel_interp(self.p_x[p]) * (0.5 * dt)
            self.p_x[p] += self.vel_interp(midpos) * dt

    @ti.kernel
    def apply_markers(self):
        for I in ti.grouped(self.cell_type):
            if self.cell_type[I] != utils.SOLID:
                self.cell_type[I] = utils.AIR

        for I in ti.grouped(self.cell_type):
            if self.cell_type[I] != utils.SOLID and self.level_set.phi[I] <= 0:
                self.cell_type[I] = utils.FLUID

    @ti.kernel
    def add_gravity(self, dt : ti.f32):
        for k in ti.static(range(self.dim)):
            if ti.static(self.gravity[k] != 0):
                g = self.gravity[k]
                for I in ti.grouped(self.velocity[k]):
                    self.velocity[k][I] += g * dt

    @ti.kernel
    def enforce_boundary(self):
        for I in ti.grouped(self.cell_type):
            if self.cell_type[I] == utils.SOLID:
                for k in ti.static(range(self.dim)):
                    self.velocity[k][I] = 0
                    self.velocity[k][I + ti.Vector.unit(self.dim, k)] = 0

    def solve_pressure(self, dt, strategy):
        strategy.scale_A = dt / (self.rho * self.dx * self.dx)
        strategy.scale_b = 1 / self.dx

        start1 = time.perf_counter()
        self.poisson_solver.reinitialize(self.cell_type, strategy)
        end1 = time.perf_counter()

        start2 = time.perf_counter()
        self.poisson_solver.solve(self.iterations, self.verbose)
        end2 = time.perf_counter()

        print(f'\033[33minit cost {end1 - start1}s, solve cost {end2 - start2}s\033[0m')
        self.pressure.copy_from(self.poisson_solver.x)

    @ti.kernel
    def apply_pressure(self, dt : ti.f32):
        scale = dt / (self.rho * self.dx)

        for k in ti.static(range(self.dim)):
            for I in ti.grouped(self.cell_type):
                I_1 = I - ti.Vector.unit(self.dim, k)
                if self.is_fluid(I_1) or self.is_fluid(I):
                    if self.is_solid(I_1) or self.is_solid(I): self.velocity[k][I] = 0
                    # FLuid-Air
                    elif self.is_air(I): 
                        if ti.static(self.ghost_fluid_method):
                            c = (self.level_set.phi[I_1] - self.level_set.phi[I]) / self.level_set.phi[I_1]
                            self.velocity[k][I] -= scale * (self.p0 - self.pressure[I_1]) * min(c, 1e3) # # limit the coefficient
                        else: self.velocity[k][I] -= scale * (self.p0 - self.pressure[I_1])
                    # Air-Fluid
                    elif self.is_air(I_1):
                        if ti.static(self.ghost_fluid_method):
                            c = (self.level_set.phi[I] - self.level_set.phi[I_1]) / self.level_set.phi[I]
                            self.velocity[k][I] -= scale * (self.pressure[I] - self.p0) * min(c, 1e3)
                        else: self.velocity[k][I] -= scale * (self.pressure[I] - self.p0)
                    # Fluid-Fluid
                    else: self.velocity[k][I] -= scale * (self.pressure[I] - self.pressure[I_1])

    @ti.func
    def advect(self, I, dst, src, offset, dt):
        pos = (I + offset) * self.dx
        midpos = pos - self.vel_interp(pos) * (0.5 * dt)
        p0 = pos - self.vel_interp(midpos) * dt
        dst[I] = utils.sample(src, p0 / self.dx - offset)

    @ti.kernel
    def advect_quantity(self, dt : ti.f32):
        if ti.static(self.solver_type == ADVECT_REDISTANCE):
            for I in ti.grouped(self.level_set.phi):
                self.advect(I, self.level_set.phi_temp, self.level_set.phi, 0.5, dt)

        for k in ti.static(range(self.dim)):
            offset = 0.5 * (1 - ti.Vector.unit(self.dim, k))
            for I in ti.grouped(self.velocity_backup[k]):
                self.advect(I, self.velocity_backup[k], self.velocity[k], offset, dt)

    def update_quantity(self):
        if ti.static(self.solver_type == ADVECT_REDISTANCE):
            self.level_set.phi.copy_from(self.level_set.phi_temp)
        for k in range(self.dim):
            self.velocity[k].copy_from(self.velocity_backup[k]) 

    @ti.kernel
    def mark_valid(self, k : ti.template()):
        for I in ti.grouped(self.velocity[k]):
            # NOTE that the the air-liquid interface is valid
            I_1 = I - ti.Vector.unit(self.dim, k)
            if self.is_fluid(I_1) or self.is_fluid(I):
                self.valid[I] = 1
            else:
                self.valid[I] = 0

    @ti.kernel
    def diffuse_quantity(self, dst : ti.template(), src : ti.template(), valid_dst : ti.template(), valid : ti.template()):
        for I in ti.grouped(dst):
            if valid[I] == 0:
                tot = ti.cast(0, self.real)
                cnt = 0
                for offset in ti.static(ti.grouped(ti.ndrange(*((-1, 2), ) * self.dim))):
                    if valid[I + offset] == 1:
                        tot += src[I + offset]
                        cnt += 1
                if cnt > 0:
                    dst[I] = tot / ti.cast(cnt, self.real)
                    valid_dst[I] = 1

    def extrap_velocity(self):
        for k in range(self.dim):
            self.mark_valid(k)
            for i in range(10):
                self.velocity_backup[k].copy_from(self.velocity[k])
                self.valid_temp.copy_from(self.valid)
                self.diffuse_quantity(self.velocity[k], self.velocity_backup[k], self.valid, self.valid_temp)

    def begin_substep(self, dt):
        self.advect_markers(dt)
        self.advect_quantity(dt)
        self.update_quantity()

        if self.solver_type == MARKERS:
            self.level_set.build_from_markers(self.p_x, self.total_mk)
        else:
            self.level_set.redistance()

        self.apply_markers()
        self.enforce_boundary()

        if self.verbose:
            mks = max(np.max(self.velocity[0].to_numpy()), np.max(self.velocity[1].to_numpy()))
            print(f'\033[36mMax advect velocity: {mks}\033[0m')
        
    def end_substep(self, dt):
        self.extrap_velocity()
        self.enforce_boundary()

        self.total_t += self.dt

    def substep(self, dt):
        self.begin_substep(dt)

        self.add_gravity(dt)
        self.enforce_boundary()

        self.extrap_velocity()
        self.enforce_boundary()

        self.solve_pressure(dt, self.strategy)
        if self.verbose:
            prs = np.max(self.pressure.to_numpy())
            print(f'\033[36mMax pressure: {prs}\033[0m')
        self.apply_pressure(dt)
        self.extrap_velocity()
        self.enforce_boundary()

        self.end_substep(dt)
 
    def run(self, max_steps, visualizer, verbose = True):
        self.verbose = verbose
        step = 0

        while step < max_steps or max_steps == -1:
            print(f'Current progress: ({step} / {max_steps})')
            for substep in range(self.substeps):
                self.substep(self.dt)
            visualizer.visualize(self)
            step += 1

    @ti.kernel
    def init_boundary(self):
        for I in ti.grouped(self.cell_type):
            if any(I == 0) or any(I + 1 == self.res):
                self.cell_type[I] = utils.SOLID

    @ti.kernel
    def init_markers(self):
        self.total_mk[None] = 0
        for I in ti.grouped(self.cell_type):
            if self.cell_type[I] == utils.FLUID:
                for offset in ti.static(ti.grouped(ti.ndrange(*((0, self.p_per_axis), ) * self.dim))):
                    num = ti.atomic_add(self.total_mk[None], 1)
                    self.p_x[num] = (I + (offset + [ti.random() for _ in ti.static(range(self.dim))]) / self.p_per_axis) * self.dx

    @ti.kernel
    def reinitialize(self):
        for I in ti.grouped(ti.ndrange(* [self.res[_] for _ in range(self.dim)])):
            self.cell_type[I] = 0
            self.pressure[I] = 0
            for k in ti.static(range(self.dim)):
                I_1 = I + ti.Vector.unit(self.dim, k)
                self.velocity[k][I] = 0
                self.velocity[k][I_1] = 0
                self.velocity_backup[k][I] = 0
                self.velocity_backup[k][I_1] = 0

    def initialize(self, initializer):
        self.reinitialize()

        self.cell_type.fill(utils.AIR)
        initializer.init_scene(self) 

        self.init_boundary()
        self.init_markers()
