"""
The basic example is taken from "Langtangen, Hans Petter, and Anders Logg. Solving PDEs in Python: The FEniCS
Tutorial I. Springer International Publishing, 2016."

The example code has been extended with preCICE API calls and mixed boundary conditions to allow for a Dirichlet-Neumann
coupling of two separate heat equations.

The original source code can be found on https://github.com/hplgit/fenics-tutorial/blob/master/pub/python/vol1/ft03_heat.py.

Heat equation with Dirichlet conditions. (Dirichlet problem)
  u'= Laplace(u) + f  in the unit square [0,1] x [0,1]
  u = u_C             on the coupling boundary at x = 1
  u = u_D             on the remaining boundary
  u = u_0             at t = 0
  u = 1 + x^2 + alpha*y^2 + \beta*t
  f = beta - 2 - 2*alpha

Heat equation with mixed boundary conditions. (Neumann problem)
  u'= Laplace(u) + f  in the shifted unit square [1,2] x [0,1]
  du/dn = f_N         on the coupling boundary at x = 1
  u = u_D             on the remaining boundary
  u = u_0             at t = 0
  u = 1 + x^2 + alpha*y^2 + \beta*t
  f = beta - 2 - 2*alpha
"""

from __future__ import print_function, division
from fenics import Function, SubDomain, RectangleMesh, FunctionSpace, Point, Expression, Constant, DirichletBC, \
    TrialFunction, TestFunction, File, solve, plot, lhs, rhs, grad, inner, dot, dx, ds, assemble, interpolate, project, near
from enum import Enum
from fenicsadapter import Adapter
from errorcomputation import compute_errors
import argparse
import numpy as np
import os
from tools.coupling_schemes import CouplingScheme


class ProblemType(Enum):
    """
    Enum defines problem type. Details see above.
    """
    DIRICHLET = 1  # Dirichlet problem
    NEUMANN = 2  # Neumann problem


class ComplementaryBoundary(SubDomain):
    def __init__(self, subdomain):
        self.complement = subdomain
        SubDomain.__init__(self)

    def inside(self, x, on_boundary):
        tol = 1E-14
        if on_boundary and not self.complement.inside(x, on_boundary):
            return True
        else:
            return False


class CouplingBoundary(SubDomain):
    def inside(self, x, on_boundary):
        tol = 1E-14
        if on_boundary and near(x[0], x_coupling, tol) and not near(x[1], y_bottom, tol) and not near(x[1], y_top, tol):
            return True
        else:
            return False


def fluxes_from_temperature_full_domain(F, V):
    """
    compute flux from weak form (see p.3 in Toselli, Andrea, and Olof Widlund. Domain decomposition methods-algorithms and theory. Vol. 34. Springer Science & Business Media, 2006.)
    :param F: weak form with known u^{n+1}
    :param V: function space
    :param hy: spatial resolution perpendicular to flux direction
    :return:
    """
    fluxes_vector = assemble(F)  # assemble weak form -> evaluate integral
    v = TestFunction(V)
    fluxes = Function(V)  # create function for flux
    area = assemble(v * ds).get_local()
    for i in range(area.shape[0]):
        if area[i] != 0:  # put weight from assemble on function
            fluxes.vector()[i] = fluxes_vector[i] / area[i]  # scale by surface area
        else:
            assert(abs(fluxes_vector[i]) < 10**-10)  # for non surface parts, we expect zero flux
            fluxes.vector()[i] = fluxes_vector[i]
    return fluxes


parser = argparse.ArgumentParser()
parser.add_argument("-d", "--dirichlet", help="create a dirichlet problem", dest='dirichlet', action='store_true')
parser.add_argument("-n", "--neumann", help="create a neumann problem", dest='neumann', action='store_true')
parser.add_argument("-wr", "--waveform", nargs=2, default=[1, 1], type=int)
parser.add_argument("-dT", "--window-size", default=1.0, type=float)
parser.add_argument("-cpl", "--coupling-scheme", default=CouplingScheme.SERIAL_FIRST_DIRICHLET.name, type=str)

args = parser.parse_args()

# coupling parameters
if args.dirichlet:
    problem = ProblemType.DIRICHLET
if args.neumann:
    problem = ProblemType.NEUMANN
if args.dirichlet and args.neumann:
    raise Exception("you can only choose either a dirichlet problem (option -d) or a neumann problem (option -n)")
if not (args.dirichlet or args.neumann):
    raise Exception("you have to choose either a dirichlet problem (option -d) or a neumann problem (option -n)")

# Create mesh and define function space

nx = 10
ny = 10

error_tol = 10 ** -12

wr_tag = "WR{wr1}{wr2}".format(wr1=args.waveform[0], wr2=args.waveform[1])
window_size = "dT{dT}".format(dT=args.window_size)
coupling_scheme = "{}".format(args.coupling_scheme)
d_subcycling = "D".format(wr_tag=wr_tag)
n_subcycling = "N".format(wr_tag=wr_tag)

configs_path = os.path.join("experiments", wr_tag, window_size, coupling_scheme)

if problem is ProblemType.DIRICHLET:
    nx = nx*3
    adapter_config_filename = os.path.join(configs_path, "precice-adapter-config-D.json")
    other_adapter_config_filename = os.path.join(configs_path, "precice-adapter-config-N.json")

elif problem is ProblemType.NEUMANN:
    adapter_config_filename = os.path.join(configs_path, "precice-adapter-config-N.json")
    other_adapter_config_filename = os.path.join(configs_path, "precice-adapter-config-D.json")

alpha = 3  # parameter alpha
beta = 1.3  # parameter beta
gamma = 0.0  # parameter gamma, dependence of heat flux on time
y_bottom, y_top = 0, 1
x_left, x_right = 0, 2
x_coupling = 1.5  # x coordinate of coupling interface

if problem is ProblemType.DIRICHLET:
    p0 = Point(x_left, y_bottom)
    p1 = Point(x_coupling, y_top)
elif problem is ProblemType.NEUMANN:
    p0 = Point(x_coupling, y_bottom)
    p1 = Point(x_right, y_top)

mesh = RectangleMesh(p0, p1, nx, ny)
V = FunctionSpace(mesh, 'P', 1)

# Define boundary condition
u_D = Expression('1 + gamma*t*x[0]*x[0] + (1-gamma)*x[0]*x[0] + alpha*x[1]*x[1] + beta*t', degree=2, alpha=alpha, beta=beta, gamma=gamma, t=0)
u_D_function = interpolate(u_D, V)
# Define flux in x direction on coupling interface (grad(u_D) in normal direction)
f_N = Expression('2 * gamma*t*x[0] + 2 * (1-gamma)*x[0] ', degree=1, gamma=gamma, t=0)
f_N_function = interpolate(f_N, V)

coupling_boundary = CouplingBoundary()
remaining_boundary = ComplementaryBoundary(coupling_boundary)

bcs = [DirichletBC(V, u_D, remaining_boundary)]
# Define initial value
u_n = interpolate(u_D, V)
u_n.rename("Temperature", "")

precice = Adapter(adapter_config_filename, other_adapter_config_filename)  # todo: how to avoid requiring both configs without Waveform Relaxation?

if problem is ProblemType.DIRICHLET:
    dt = precice.initialize(coupling_subdomain=coupling_boundary, mesh=mesh, read_field=u_D_function,
                                    write_field=f_N_function, u_n=u_n)
elif problem is ProblemType.NEUMANN:
    dt = precice.initialize(coupling_subdomain=coupling_boundary, mesh=mesh, read_field=f_N_function,
                                    write_field=u_D_function, u_n=u_n)

# Define variational problem
u = TrialFunction(V)
v = TestFunction(V)
f = Expression('beta + gamma * x[0] * x[0] - 2 * gamma * t - 2 * (1-gamma) - 2 * alpha', degree=2, alpha=alpha, beta=beta, gamma=gamma, t=0)
F = u * v / dt * dx + dot(grad(u), grad(v)) * dx - (u_n / dt + f) * v * dx

if problem is ProblemType.DIRICHLET:
    # apply Dirichlet boundary condition on coupling interface
    bcs.append(precice.create_coupling_dirichlet_boundary_condition(V))
if problem is ProblemType.NEUMANN:
    # apply Neumann boundary condition on coupling interface, modify weak form correspondingly
    F += precice.create_coupling_neumann_boundary_condition(v)

a, L = lhs(F), rhs(F)

# Time-stepping
u_np1 = Function(V)
F_known_u = u_np1 * v / dt * dx + dot(grad(u_np1), grad(v)) * dx - (u_n / dt + f) * v * dx
u_np1.rename("Temperature", "")
t = 0

# reference solution at t=0
u_ref = interpolate(u_D, V)
u_ref.rename("reference", " ")

temperature_out = File("out/%s.pvd" % precice._solver_name)
ref_out = File("out/ref%s.pvd" % precice._solver_name)
error_out = File("out/error%s.pvd" % precice._solver_name)

# output solution and reference solution at t=0, n=0
n = 0
temperature_out << u_n
ref_out << u_ref

error_total, error_pointwise = compute_errors(u_n, u_ref, V)
error_out << error_pointwise

# set t_1 = t_0 + dt, this gives u_D^1
u_D.t = t + dt(0)  # call dt(0) to evaluate FEniCS Constant. Todo: is there a better way?
f.t = t + dt(0)

while precice.is_coupling_ongoing():

    x_check, y_check = 1.5, 0.5

    # Compute solution u^n+1, use bcs u_D^n+1, u^n and coupling bcs
    solve(a == L, u_np1, bcs)

    if problem is ProblemType.DIRICHLET:
        # Dirichlet problem obtains flux from solution and sends flux on boundary to Neumann problem
        fluxes = fluxes_from_temperature_full_domain(F_known_u, V)
        t, n, precice_timestep_complete, precice_dt = precice.advance(fluxes, u_np1, u_n, t, dt(0), n)
    elif problem is ProblemType.NEUMANN:
        # Neumann problem samples temperature on boundary from solution and sends temperature to Dirichlet problem
        t, n, precice_timestep_complete, precice_dt = precice.advance(u_np1, u_np1, u_n, t, dt(0), n)

    dt.assign(np.min([precice.fenics_dt, precice_dt]))  # todo we could also consider deciding on time stepping size inside the adapter

    if precice_timestep_complete:
        u_ref = interpolate(u_D, V)
        u_ref.rename("reference", " ")
        error, error_pointwise = compute_errors(u_n, u_ref, V, total_error_tol=error_tol)
        # output solution and reference solution at t_n+1
        temperature_out << u_n
        ref_out << u_ref
        error_out << error_pointwise

    # Update dirichlet BC
    u_D.t = t + dt(0)
    f.t = t + dt(0)

# Hold plot
precice.finalize()
