import argparse

import dolfinx
import dolfinx.io
import numpy as np
import ufl
from mpi4py import MPI

from nitsche_one_way import nitsche_one_way
from snes_disk_against_plane import snes_solver


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Nitsche's metood for contact against a straight plane"
                                     + " with PETSc SNES",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--theta", default=1, type=np.float64, dest="theta",
                        help="Theta parameter for Nitsche, 1 symmetric, -1 skew symmetric, 0 Penalty-like")
    parser.add_argument("--gamma", default=1000, type=np.float64, dest="gamma",
                        help="Coercivity/Stabilization parameter for Nitsche condition")
    _solve = parser.add_mutually_exclusive_group(required=False)
    _solve.add_argument('--linear', dest='linear_solver', action='store_true',
                        help="Use linear solver", default=False)
    _strain = parser.add_mutually_exclusive_group(required=False)
    _strain.add_argument('--strain', dest='plane_strain', action='store_true',
                         help="Use plane strain formulation", default=False)
    _dirichlet = parser.add_mutually_exclusive_group(required=False)
    _dirichlet.add_argument('--dirichlet', dest='dirichlet', action='store_true',
                            help="Use strong Dirichlet formulation", default=False)
    _mesh = parser.add_mutually_exclusive_group(required=False)
    _mesh.add_argument("--square", dest="square", action='store_true',
                       help="Use square mesh (instead of circular mesh).")
    _E = parser.add_argument("--E", default=1e3, type=np.float64, dest="E",
                             help="Youngs modulus of material")
    _nu = parser.add_argument(
        "--nu", default=0.1, type=np.float64, dest="nu", help="Poisson's ratio")
    _disp = parser.add_argument("--disp", default=0.08, type=np.float64, dest="disp",
                                help="Displacement BC in negative y direction")
    _ref = parser.add_argument("--refinements", default=2, type=np.int32,
                               dest="refs", help="Number of mesh refinements")
    _gap = parser.add_argument(
        "--gap", default=0.02, type=np.float64, dest="gap", help="Gap between plane and y=0")
    # Parse input arguments or set to defualt values
    args = parser.parse_args()

    # Current formulation uses unilateral contact, i.e. s is unused
    nitsche_parameters = {"gamma": args.gamma, "theta": args.theta, "s": 0}
    nitsche_bc = not args.dirichlet
    disk = not args.square
    physical_parameters = {"E": args.E,
                           "nu": args.nu, "strain": args.plane_strain}
    vertical_displacement = -args.disp
    num_refs = args.refs
    gap = args.gap
    top_value = 1
    bottom_value = 2

    # Load mesh and create identifier functions for the top (Displacement condition)
    # and the bottom (contact condition)
    if disk:
        with dolfinx.io.XDMFFile(MPI.COMM_WORLD, "disk.xdmf", "r") as xdmf:
            mesh = xdmf.read_mesh(name="Grid")

        def top(x):
            return x[1] > 0.5

        def bottom(x):
            return x[1] < 0.2
    else:
        mesh = dolfinx.UnitSquareMesh(MPI.COMM_WORLD, 50, 50)

        def top(x):
            return np.isclose(x[1], 1)

        def bottom(x):
            return np.isclose(x[1], 0)

    e_abs = []
    e_rel = []

    for i in range(0, num_refs):
        if i > 0:
            # Refine mesh
            mesh.topology.create_entities(mesh.topology.dim - 1)
            mesh = dolfinx.mesh.refine(mesh)
        # Create meshtag for top and bottom markers
        tdim = mesh.topology.dim
        top_facets = dolfinx.mesh.locate_entities_boundary(mesh, tdim - 1, top)
        bottom_facets = dolfinx.mesh.locate_entities_boundary(
            mesh, tdim - 1, bottom)
        top_values = np.full(len(top_facets), top_value, dtype=np.int32)
        bottom_values = np.full(
            len(bottom_facets), bottom_value, dtype=np.int32)
        indices = np.concatenate([top_facets, bottom_facets])
        values = np.hstack([top_values, bottom_values])
        facet_marker = dolfinx.MeshTags(mesh, tdim - 1, indices, values)
        mesh_data = (facet_marker, top_value, bottom_value)

        # Solve contact problem using Nitsche's method
        u1 = nitsche_one_way(mesh=mesh, mesh_data=mesh_data, physical_parameters=physical_parameters,
                             vertical_displacement=vertical_displacement, refinement=i, g=gap, nitsche_bc=True)
        # Solve contact problem using PETSc SNES
        u2 = snes_solver(mesh=mesh, mesh_data=mesh_data, physical_parameters=physical_parameters,
                         vertical_displacement=vertical_displacement, refinement=i, g=gap)

        # Compute the difference (error) between Nitsche and SNES
        V = u1.function_space
        dx = ufl.Measure("dx", domain=mesh)
        error = ufl.inner(u1 - u2, u1 - u2) * dx
        E_L2 = np.sqrt(dolfinx.fem.assemble_scalar(error))
        u2_norm = ufl.inner(u2, u2) * dx
        u2_L2 = np.sqrt(dolfinx.fem.assemble_scalar(u2_norm))
        print(f"abs. L2-error={E_L2:.2e}")
        print(f"rel. L2-error={E_L2/u2_L2:.2e}")
        e_abs.append(E_L2)
        e_rel.append(E_L2 / u2_L2)
    print(f"Absolute error {e_abs}")
    print(f"Relative error {e_rel}")
    assert(e_rel[-1] < 1e-4)
