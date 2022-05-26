# Copyright (C) 2021 Jørgen S. Dokken and Sarah Roggendorf
#
# SPDX-License-Identifier:    MIT

from typing import Tuple, Dict

import dolfinx.common as _common
import dolfinx.cpp as _cpp
import dolfinx.fem as _fem
import dolfinx.geometry as _geometry
import dolfinx.log as _log
import dolfinx.mesh as _mesh
import dolfinx.nls as _nls
import numpy as np
import ufl
from dolfinx.cpp.mesh import MeshTags_int32
from petsc4py import PETSc as _PETSc

import dolfinx_contact.cpp
from dolfinx_contact.helpers import (R_minus, epsilon, lame_parameters,
                                     rigid_motions_nullspace, sigma_func)


def nitsche_rigid_surface(mesh: _mesh.Mesh, mesh_data: Tuple[MeshTags_int32, int, int, int, int],
                          physical_parameters: dict = {}, nitsche_parameters: Dict[str, float] = {},
                          vertical_displacement: float = -0.1, nitsche_bc: bool = False, quadrature_degree: int = 5,
                          form_compiler_params: Dict = {}, jit_params: Dict = {}, petsc_options: Dict = {},
                          newton_options: Dict = {}):
    """
    Use custom kernel to compute the one sided contact problem with a mesh coming into contact
    with a rigid surface (meshed) with constant normal n_2.

    Parameters
    ==========
    mesh
        The input mesh
    mesh_data
        A quinteplet with a mesh tag for facets and values v0, v1, v2, v3. v0 and v3
        should be the values in the mesh tags for facets to apply a Dirichlet condition
        on, where v0 corresponds to the elastic body and v2 to the rigid body. v1 is the
        value for facets which should have applied a contact condition on and v2 marks
        the potential contact surface on the rigid body.
    physical_parameters
        Optional dictionary with information about the linear elasticity problem.
        Valid (key, value) tuples are: ('E': float), ('nu', float), ('strain', bool)
    nitsche_parameters
        Optional dictionary with information about the Nitsche configuration.
        Valid (keu, value) tuples are: ('gamma', float), ('theta', float) where theta can be -1, 0 or 1 for
        skew-symmetric, penalty like or symmetric enforcement of Nitsche conditions
    vertical_displacement
        The amount of verticial displacment enforced on Dirichlet boundary
    nitsche_bc
        Use Nitche's method to enforce Dirichlet boundary conditions
    quadrature_degree
        The quadrature degree to use for the custom contact kernels
    form_compiler_params
        Parameters used in FFCX compilation of this form. Run `ffcx --help` at
        the commandline to see all available opdirichlet_value_rigidtions. Takes priority over all
        other parameter values, except for `scalar_type` which is determined by
        DOLFINX.
    jit_params
        Parameters used in CFFI JIT compilation of C code generated by FFCX.
        See https://github.com/FEniCS/dolfinx/blob/main/python/dolfinx/jit.py
        for all available parameters. Takes priority over all other parameter values.
    petsc_options
        Parameters that is passed to the linear algebra backend
        PETSc. For available choices for the 'petsc_options' kwarg,
        see the `PETSc-documentation
        <https://petsc4py.readthedocs.io/en/stable/manual/ksp/>`
    newton_options
        Dictionary with Newton-solver options. Valid (key, item) tuples are:
        ("atol", float), ("rtol", float), ("convergence_criterion", "str"),
        ("max_it", int), ("error_on_nonconvergence", bool), ("relaxation_parameter", float)
    """

    # Compute lame parameters
    plane_strain = physical_parameters.get("strain", False)
    E = physical_parameters.get("E", 1e3)
    nu = physical_parameters.get("nu", 0.1)
    mu_func, lambda_func = lame_parameters(plane_strain)
    mu = mu_func(E, nu)
    lmbda = lambda_func(E, nu)
    sigma = sigma_func(mu, lmbda)

    # Nitsche parameters and variables
    theta = nitsche_parameters.get("theta", 1)
    gamma = nitsche_parameters.get("gamma", 10)

    # Unpack mesh data
    (facet_marker, dirichlet_value_elastic, contact_value_elastic, contact_value_rigid,
     dirichlet_value_rigid) = mesh_data
    assert(facet_marker.dim == mesh.topology.dim - 1)
    gdim = mesh.geometry.dim

    # Setup function space and functions used in Jacobian and residual formulation
    V = _fem.VectorFunctionSpace(mesh, ("CG", 1))
    u = _fem.Function(V)
    v = ufl.TestFunction(V)
    du = ufl.TrialFunction(V)
    u = _fem.Function(V)
    v = ufl.TestFunction(V)

    # Compute classical (volume) contributions of the equations of linear elasticity
    dx = ufl.Measure("dx", domain=mesh)
    ds = ufl.Measure("ds", domain=mesh, subdomain_data=facet_marker)
    h = ufl.Circumradius(mesh)
    n = ufl.FacetNormal(mesh)
    J = ufl.inner(sigma(du), epsilon(v)) * dx
    F = ufl.inner(sigma(u), epsilon(v)) * dx

    # Nitsche for Dirichlet, another theta-scheme.
    # https://doi.org/10.1016/j.cma.2018.05.024
    if nitsche_bc:
        disp_vec = np.zeros(gdim)
        disp_vec[gdim - 1] = vertical_displacement
        u_D = ufl.as_vector(disp_vec)
        F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_elastic)\
             - theta * ufl.inner(sigma(v) * n, u - u_D) * \
            ds(dirichlet_value_elastic) + E * gamma / h * ufl.inner(u - u_D, v) * ds(dirichlet_value_elastic)

        J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_elastic)\
            - theta * ufl.inner(sigma(v) * n, du) * \
            ds(dirichlet_value_elastic) + E * gamma / h * ufl.inner(du, v) * ds(dirichlet_value_elastic)

        # Nitsche bc for rigid plane
        disp_plane = np.zeros(gdim)
        u_D_plane = ufl.as_vector(disp_plane)
        F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_rigid)\
             - theta * ufl.inner(sigma(v) * n, u - u_D_plane) * \
            ds(dirichlet_value_rigid) + E * gamma / h * ufl.inner(u - u_D_plane, v) * ds(dirichlet_value_rigid)
        J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_rigid)\
            - theta * ufl.inner(sigma(v) * n, du) * \
            ds(dirichlet_value_rigid) + E * gamma / h * ufl.inner(du, v) * ds(dirichlet_value_rigid)
        bcs = []
    else:
        # strong Dirichlet boundary conditions
        def _u_D(x):
            values = np.zeros((mesh.geometry.dim, x.shape[1]))
            values[mesh.geometry.dim - 1] = vertical_displacement
            return values
        tdim = mesh.topology.dim
        u_D = _fem.Function(V)
        u_D.interpolate(_u_D)
        u_D.name = "u_D"
        u_D.x.scatter_forward()
        dirichlet_dofs = _fem.locate_dofs_topological(
            V, tdim - 1, facet_marker.indices[facet_marker.values == dirichlet_value_elastic])
        bc = _fem.dirichletbc(u_D, dirichlet_dofs)
        bcs = [bc]
        # Dirichlet boundary conditions for rigid plane
        dirichlet_dofs_plane = _fem.locate_dofs_topological(
            V, tdim - 1, facet_marker.indices[facet_marker.values == dirichlet_value_rigid])
        u_D_plane = _fem.Function(V)
        with u_D_plane.vector.localForm() as loc:
            loc.set(0)
        bc_plane = _fem.dirichletbc(u_D_plane, dirichlet_dofs_plane)
        bcs.append(bc_plane)

    # Create contact class
    contact_facets = facet_marker.indices[facet_marker.values == contact_value_elastic]
    contact = dolfinx_contact.cpp.Contact(facet_marker, [contact_value_elastic, contact_value_rigid], V._cpp_object)
    # Ensures that we find closest facet to midpoint of facet
    contact.set_quadrature_degree(1)

    # Create gap function
    gdim = mesh.geometry.dim
    fdim = mesh.topology.dim - 1
    mesh_geometry = mesh.geometry.x
    contact.create_distance_map(0, 1)
    lookup = contact.facet_map(0)
    master_bbox = _geometry.BoundingBoxTree(mesh, fdim, contact_facets)
    midpoint_tree = _geometry.create_midpoint_tree(mesh, fdim, contact_facets)

    # This function returns Pi(x) - x, where Pi(x) is the closest point projection
    def gap(x):
        dist_vec_array = np.zeros((gdim, x.shape[1]))
        # Find closest facet to point x
        facets = _geometry.compute_closest_entity(master_bbox, midpoint_tree, mesh, np.transpose(x))
        for i in range(x.shape[1]):
            xi = x[:, i]
            facet = facets[i]
            # Compute distance between point and closest facet
            index = np.argwhere(np.array(contact_facets) == facet)[0, 0]
            facet_geometry = _cpp.mesh.entities_to_geometry(mesh, fdim, [facet], False)
            coords0 = mesh_geometry[facet_geometry][0]
            R = np.linalg.norm(_cpp.geometry.compute_distance_gjk(coords0, xi))
            # If point on a facet in contact surface (i.e., if distance between point and closest
            # facet is 0), use contact.facet_map(0) to find closest facet on rigid surface and
            # compute distance vector
            if np.isclose(R, 0):
                facet_2 = lookup.links(index)[0]
                facet2_geometry = _cpp.mesh.entities_to_geometry(mesh, fdim, [facet_2], False)
                coords = mesh_geometry[facet2_geometry][0]
                dist_vec = _cpp.geometry.compute_distance_gjk(coords, xi)
                dist_vec_array[: gdim, i] = dist_vec[: gdim]
        return dist_vec_array

    # interpolate gap function
    g_vec = _fem.Function(V)
    g_vec.interpolate(gap)

    # Normal vector pointing into plane (but outward of the body coming into contact)
    # Similar to computing the normal by finding the gap vector between two meshes
    n_vec = np.zeros(mesh.geometry.dim)
    n_vec[mesh.geometry.dim - 1] = -1
    n_2 = ufl.as_vector(n_vec)  # Normal of plane (projection onto other body)

    # Define sigma_n
    def sigma_n(v):
        # NOTE: Different normals, see summary paper
        return ufl.dot(sigma(v) * n, n_2)

    # # Derivation of one sided Nitsche with gap function
    gamma = gamma * E / h
    F = F - theta / gamma * sigma_n(u) * sigma_n(v) * ds(contact_value_elastic)
    F += 1 / gamma * R_minus(sigma_n(u) + gamma * (ufl.dot(g_vec, n_2) - ufl.dot(u, n_2))) * \
        (theta * sigma_n(v) - gamma * ufl.dot(v, n_2)) * ds(contact_value_elastic)
    q = sigma_n(u) + gamma * (ufl.dot(g_vec, n_2) - ufl.dot(u, n_2))
    J = J - theta / gamma * sigma_n(du) * sigma_n(v) * ds(contact_value_elastic)
    J += 1 / gamma * 0.5 * (1 - ufl.sign(q)) * (sigma_n(du) - gamma * ufl.dot(du, n_2)) * \
        (theta * sigma_n(v) - gamma * ufl.dot(v, n_2)) * ds(contact_value_elastic)

    # Setup non-linear problem and Newton-solver
    problem = _fem.petsc.NonlinearProblem(F, u, bcs, J=J)
    solver = _nls.petsc.NewtonSolver(mesh.comm, problem)

    # Create rigid motion null-space
    null_space = rigid_motions_nullspace(V)
    solver.A.setNearNullSpace(null_space)

    # Set Newton solver options
    solver.atol = newton_options.get("atol", 1e-9)
    solver.rtol = newton_options.get("rtol", 1e-9)
    solver.convergence_criterion = newton_options.get("convergence_criterion", "incremental")
    solver.max_it = newton_options.get("max_it", 50)
    solver.error_on_nonconvergence = newton_options.get("error_on_nonconvergence", True)
    solver.relaxation_parameter = newton_options.get("relaxation_parameter", 1.0)

    # Set initial condition
    def _u_initial(x):
        values = np.zeros((gdim, x.shape[1]))
        values[-1] = -0.01
        return values
    u.interpolate(_u_initial)

    # Define solver and options
    ksp = solver.krylov_solver
    opts = _PETSc.Options()
    option_prefix = ksp.getOptionsPrefix()

    # Set PETSc options
    opts = _PETSc.Options()
    opts.prefixPush(option_prefix)
    for k, v in petsc_options.items():
        opts[k] = v
    opts.prefixPop()
    ksp.setFromOptions()

    dofs_global = V.dofmap.index_map_bs * V.dofmap.index_map.size_global
    _log.set_log_level(_log.LogLevel.INFO)

    # Solve non-linear problem
    with _common.Timer(f"{dofs_global} Solve Nitsche"):
        n, converged = solver.solve(u)
    u.x.scatter_forward()

    if solver.error_on_nonconvergence:
        assert(converged)
    print(f"{dofs_global}, Number of interations: {n:d}")

    return u
