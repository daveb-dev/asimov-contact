# Copyright (C) 2021 Jørgen S. Dokken and Sarah Roggendorf
#
# SPDX-License-Identifier:    MIT

import argparse

from dolfinx.mesh import MeshTags, locate_entities_boundary
from dolfinx.io import XDMFFile
from dolfinx.fem import Function, VectorFunctionSpace
import numpy as np
from mpi4py import MPI

from dolfinx_contact.nitsche_unbiased import nitsche_unbiased
from dolfinx_contact.create_contact_meshes import create_circle_plane_mesh, create_circle_circle_mesh,\
    create_sphere_plane_mesh, create_box_mesh_2D, create_box_mesh_3D
from dolfinx_contact.helpers import convert_mesh

if __name__ == "__main__":
    desc = "Nitsche's method with rigid surface using custom assemblers"
    parser = argparse.ArgumentParser(description=desc,
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--theta", default=1, type=np.float64, dest="theta",
                        help="Theta parameter for Nitsche, 1 symmetric, -1 skew symmetric, 0 Penalty-like")
    parser.add_argument("--gamma", default=10, type=np.float64, dest="gamma",
                        help="Coercivity/Stabilization parameter for Nitsche condition")
    _solve = parser.add_mutually_exclusive_group(required=False)
    _solve.add_argument('--linear', dest='linear_solver', action='store_true',
                        help="Use linear solver", default=False)
    _3D = parser.add_mutually_exclusive_group(required=False)
    _3D.add_argument('--3D', dest='threed', action='store_true',
                     help="Use 3D mesh", default=False)
    _curved = parser.add_mutually_exclusive_group(required=False)
    _curved.add_argument('--curved', dest='curved', action='store_true',
                         help="Use curved rigid surface", default=False)
    _box = parser.add_mutually_exclusive_group(required=False)
    _box.add_argument('--box', dest='box', action='store_true',
                      help="Use curved rigid surface", default=False)
    _strain = parser.add_mutually_exclusive_group(required=False)
    _strain.add_argument('--strain', dest='plane_strain', action='store_true',
                         help="Use plane strain formulation", default=False)
    _dirichlet = parser.add_mutually_exclusive_group(required=False)
    _dirichlet.add_argument('--dirichlet', dest='dirichlet', action='store_true',
                            help="Use strong Dirichlet formulation", default=False)
    _E = parser.add_argument("--E", default=1e3, type=np.float64, dest="E",
                             help="Youngs modulus of material")
    _nu = parser.add_argument(
        "--nu", default=0.1, type=np.float64, dest="nu", help="Poisson's ratio")
    _disp = parser.add_argument("--disp", default=0.2, type=np.float64, dest="disp",
                                help="Displacement BC in negative y direction")
    _ref = parser.add_argument("--refinements", default=2, type=np.int32,
                               dest="refs", help="Number of mesh refinements")
    _nload_steps = parser.add_argument("--load_steps", default=1, type=np.int32, dest="nload_steps",
                                       help="Number of steps for gradual loading")

    # Parse input arguments or set to defualt values
    args = parser.parse_args()

    # Current formulation uses unilateral contact
    nitsche_parameters = {"gamma": args.gamma, "theta": args.theta}
    nitsche_bc = not args.dirichlet
    physical_parameters = {"E": args.E, "nu": args.nu, "strain": args.plane_strain}
    vertical_displacement = -args.disp
    num_refs = args.refs + 1
    top_value = 1
    threed = args.threed
    bottom_value = 2
    curved = args.curved
    box = args.box
    nload_steps = args.nload_steps

    # Load mesh and create identifier functions for the top (Displacement condition)
    # and the bottom (contact condition)
    if threed:
        if box:
            fname = "box_3D"
            create_box_mesh_3D(filename=f"{fname}.msh")
            convert_mesh(fname, "tetra")

            with XDMFFile(MPI.COMM_WORLD, f"{fname}.xdmf", "r") as xdmf:
                mesh = xdmf.read_mesh(name="Grid")
            tdim = mesh.topology.dim
            gdim = mesh.geometry.dim
            mesh.topology.create_connectivity(tdim - 1, 0)
            mesh.topology.create_connectivity(tdim - 1, tdim)

            top_value = 1
            bottom_value = 2
            surface_value = 3
            surface_bottom = 4
            # Create meshtag for top and bottom markers
            top_facets1 = locate_entities_boundary(mesh, tdim - 1, lambda x: np.isclose(x[2], 0.5))
            bottom_facets1 = locate_entities_boundary(
                mesh, tdim - 1, lambda x: np.isclose(x[2], 0.0))
            top_facets2 = locate_entities_boundary(mesh, tdim - 1, lambda x: np.isclose(x[2], -0.1))
            bottom_facets2 = locate_entities_boundary(
                mesh, tdim - 1, lambda x: np.isclose(x[2], -0.6))
            top_values = np.full(len(top_facets1), top_value, dtype=np.int32)
            bottom_values = np.full(
                len(bottom_facets1), bottom_value, dtype=np.int32)

            surface_values = np.full(len(top_facets2), surface_value, dtype=np.int32)
            sbottom_values = np.full(
                len(bottom_facets2), surface_bottom, dtype=np.int32)
            indices = np.concatenate([top_facets1, bottom_facets1, top_facets2, bottom_facets2])
            values = np.hstack([top_values, bottom_values, surface_values, sbottom_values])
            sorted_facets = np.argsort(indices)
            facet_marker = MeshTags(mesh, tdim - 1, indices[sorted_facets], values[sorted_facets])
        else:
            fname = "sphere"
            create_sphere_plane_mesh(filename=f"{fname}.msh")
            convert_mesh(fname, "tetra")
            convert_mesh(f"{fname}", "triangle", ext="facets")
            with XDMFFile(MPI.COMM_WORLD, f"{fname}.xdmf", "r") as xdmf:
                mesh = xdmf.read_mesh(name="Grid")
            tdim = mesh.topology.dim
            mesh.topology.create_connectivity(tdim - 1, 0)
            mesh.topology.create_connectivity(tdim - 1, tdim)
            with XDMFFile(MPI.COMM_WORLD, f"{fname}_facets.xdmf", "r") as xdmf:
                facet_marker = xdmf.read_meshtags(mesh, name="Grid")
            top_value = 2
            bottom_value = 1
            surface_value = 8
            surface_bottom = 7

    else:
        if curved:
            fname = "two_disks"
            create_circle_circle_mesh(filename=f"{fname}.msh")
            convert_mesh(fname, "triangle", prune_z=True)
            convert_mesh(f"{fname}", "line", ext="facets", prune_z=True)

            with XDMFFile(MPI.COMM_WORLD, f"{fname}.xdmf", "r") as xdmf:
                mesh = xdmf.read_mesh(name="Grid")
            tdim = mesh.topology.dim
            mesh.topology.create_connectivity(tdim - 1, 0)
            mesh.topology.create_connectivity(tdim - 1, tdim)

            def top1(x):
                return x[1] > 0.55

            def bottom1(x):
                return np.logical_and(x[1] < 0.25, x[1] > 0.15)

            def top2(x):
                return np.logical_and(x[1] > 0.05, x[1] < 0.15)

            def bottom2(x):
                return x[1] < -0.35

            top_value = 1
            bottom_value = 2
            surface_value = 3
            surface_bottom = 4
            # Create meshtag for top and bottom markers
            top_facets1 = locate_entities_boundary(mesh, tdim - 1, top1)
            bottom_facets1 = locate_entities_boundary(
                mesh, tdim - 1, bottom1)
            top_facets2 = locate_entities_boundary(mesh, tdim - 1, top2)
            bottom_facets2 = locate_entities_boundary(
                mesh, tdim - 1, bottom2)
            top_values = np.full(len(top_facets1), top_value, dtype=np.int32)
            bottom_values = np.full(
                len(bottom_facets1), bottom_value, dtype=np.int32)

            surface_values = np.full(len(top_facets2), surface_value, dtype=np.int32)
            sbottom_values = np.full(
                len(bottom_facets2), surface_bottom, dtype=np.int32)
            indices = np.concatenate([top_facets1, bottom_facets1, top_facets2, bottom_facets2])
            values = np.hstack([top_values, bottom_values, surface_values, sbottom_values])
            sorted_facets = np.argsort(indices)
            facet_marker = MeshTags(mesh, tdim - 1, indices[sorted_facets], values[sorted_facets])
        elif box:
            fname = "box_2D"
            create_box_mesh_2D(filename=f"{fname}.msh")
            convert_mesh(fname, "triangle", prune_z=True)
            convert_mesh(f"{fname}", "line", ext="facets", prune_z=True)

            with XDMFFile(MPI.COMM_WORLD, f"{fname}.xdmf", "r") as xdmf:
                mesh = xdmf.read_mesh(name="Grid")
            tdim = mesh.topology.dim
            gdim = mesh.geometry.dim
            mesh.topology.create_connectivity(tdim - 1, 0)
            mesh.topology.create_connectivity(tdim - 1, tdim)
            with XDMFFile(MPI.COMM_WORLD, f"{fname}_facets.xdmf", "r") as xdmf:
                facet_marker = xdmf.read_meshtags(mesh, name="Grid")
            top_value = 5
            bottom_value = 3
            surface_value = 9
            surface_bottom = 7

        else:
            fname = "twomeshes"
            create_circle_plane_mesh(filename=f"{fname}.msh")
            convert_mesh(fname, "triangle", prune_z=True)
            convert_mesh(f"{fname}", "line", ext="facets", prune_z=True)

            with XDMFFile(MPI.COMM_WORLD, f"{fname}.xdmf", "r") as xdmf:
                mesh = xdmf.read_mesh(name="Grid")
            tdim = mesh.topology.dim
            gdim = mesh.geometry.dim
            mesh.topology.create_connectivity(tdim - 1, 0)
            mesh.topology.create_connectivity(tdim - 1, tdim)
            with XDMFFile(MPI.COMM_WORLD, f"{fname}_facets.xdmf", "r") as xdmf:
                facet_marker = xdmf.read_meshtags(mesh, name="Grid")
            top_value = 2
            bottom_value = 4
            surface_value = 9
            surface_bottom = 7

            def top(x):
                return x[1] > 0.5

            def bottom(x):
                return x[1] < np.logical_and(x[1] < 0.45, x[1] > 0.11)

            top_value = 1
            bottom_value = 2
            surface_value = 3
            surface_bottom = 4
            # Create meshtag for top and bottom markers
            top_facets1 = locate_entities_boundary(mesh, tdim - 1, top)
            bottom_facets1 = locate_entities_boundary(
                mesh, tdim - 1, bottom)
            top_facets2 = locate_entities_boundary(mesh, tdim - 1, lambda x: np.isclose(x[1], 0.1))
            bottom_facets2 = locate_entities_boundary(
                mesh, tdim - 1, lambda x: np.isclose(x[1], 0.0))
            top_values = np.full(len(top_facets1), top_value, dtype=np.int32)
            bottom_values = np.full(
                len(bottom_facets1), bottom_value, dtype=np.int32)

            surface_values = np.full(len(top_facets2), surface_value, dtype=np.int32)
            sbottom_values = np.full(
                len(bottom_facets2), surface_bottom, dtype=np.int32)
            indices = np.concatenate([top_facets1, bottom_facets1, top_facets2, bottom_facets2])
            values = np.hstack([top_values, bottom_values, surface_values, sbottom_values])
            sorted_facets = np.argsort(indices)
            facet_marker = MeshTags(mesh, tdim - 1, indices[sorted_facets], values[sorted_facets])
    e_abs = []
    e_rel = []
    dofs_global = []
    rank = MPI.COMM_WORLD.rank
    mesh_data = (facet_marker, top_value, bottom_value, surface_value, surface_bottom)
    # Solve contact problem using Nitsche's method
    load_increment = vertical_displacement / nload_steps
    u1 = None

    V = VectorFunctionSpace(mesh, ("CG", 1))
    u = Function(V)
    u.x.array[:] = np.zeros(u.x.array[:].shape)
    geometry = mesh.geometry.x[:].copy()
    for j in range(nload_steps):
        displacement = load_increment
        # Solve contact problem using Nitsche's method
        u1 = nitsche_unbiased(mesh=mesh, mesh_data=mesh_data, physical_parameters=physical_parameters,
                              nitsche_parameters=nitsche_parameters, vertical_displacement=displacement,
                              nitsche_bc=True, initGuess=None, refinement=j)
        delta_x = u1.compute_point_values()
        u.x.array[:] += u1.x.array[:]
        if delta_x.shape[1] < 3:
            delta_x = np.hstack([delta_x, np.zeros((delta_x.shape[0], 3 - delta_x.shape[1]))])
        mesh.geometry.x[:] += delta_x
    mesh.geometry.x[:] = geometry
    with XDMFFile(MPI.COMM_WORLD, "results/u_unbiased_total.xdmf", "w") as xdmf:
        xdmf.write_mesh(mesh)
        u.name = "u"
        xdmf.write_function(u)