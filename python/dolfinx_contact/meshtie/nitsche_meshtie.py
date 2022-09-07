# Copyright (C) 2022 Sarah Roggendorf
#
# SPDX-License-Identifier:    MIT

from typing import Tuple

import dolfinx.cpp.fem as _cppfem
import dolfinx.common as _common
import dolfinx.fem as _fem
import dolfinx.log as _log
import numpy as np
import ufl
from dolfinx.cpp.graph import AdjacencyList_int32
from dolfinx.cpp.mesh import MeshTags_int32
from petsc4py import PETSc as _PETSc

import dolfinx_contact
import dolfinx_contact.cpp
from dolfinx_contact.helpers import rigid_motions_nullspace_subdomains

kt = dolfinx_contact.cpp.Kernel


def nitsche_meshtie(lhs: ufl.Form, rhs: _fem.Function, u: _fem.Function, markers: list[MeshTags_int32],
                    surface_data: Tuple[AdjacencyList_int32, list[Tuple[int, int]]],
                    bcs: list[_fem.DirichletBCMetaClass],
                    problem_parameters: dict[str, np.float64],
                    quadrature_degree: int = 5, form_compiler_options: dict = None, jit_options: dict = None,
                    petsc_options: dict = None, timing_str: str = '') -> Tuple[_fem.Function, int, int, float]:
    """
    Use custom kernel to compute elasticity problem if mesh consists of topologically disconnected parts

    Parameters
    ==========
    lhs the variational form (bilinear form) for the stiffness matrix
    rhs the variational form  (linear form) for the right hand side
    u The function to be solved for. Also serves as initial value.
    markers
        A list of meshtags. The first element must mark all separate objects in order to create the correct nullspace.
        The second element must contain the mesh_tags for all puppet surfaces,
        Dirichlet-surfaces and Neumann-surfaces
        All further elements may contain candidate_surfaces
    contact_data = (surfaces, surface_pairs), where
        surfaces: Adjacency list. Links of i are meshtag values for contact
                  surfaces in ith mesh_tag in mesh_tags
        surface_pairs: list of pairs (i, j) marking the ith surface as a puppet
                  surface and the jth surface as the corresponding candidate
                  surface
    problem_parameters
        Dictionary with lame parameters and Nitsche parameters.
        Valid (key, value) tuples are: ('gamma': float), ('theta', float), ('mu', float),
        (lambda, float),
        where theta can be -1, 0 or 1 for skew-symmetric, penalty like or symmetric
        enforcement of Nitsche conditions
    quadrature_degree
        The quadrature degree to use for the custom contact kernels
    form_compiler_options
        Parameters used in FFCX compilation of this form. Run `ffcx --help` at
        the commandline to see all available options. Takes priority over all
        other parameter values, except for `scalar_type` which is determined by
        DOLFINX.
    jit_options
        Parameters used in CFFI JIT compilation of C code generated by FFCX.
        See https://github.com/FEniCS/dolfinx/blob/main/python/dolfinx/jit.py
        for all available parameters. Takes priority over all other parameter values.
    petsc_options
        Parameters that is passed to the linear algebra backend
        PETSc. For available choices for the 'petsc_options' kwarg,
        see the `PETSc-documentation
        <https://petsc4py.readthedocs.io/en/stable/manual/ksp/>`
    """

    form_compiler_options = {} if form_compiler_options is None else form_compiler_options
    jit_options = {} if jit_options is None else jit_options
    petsc_options = {} if petsc_options is None else petsc_options

    if problem_parameters.get("mu") is None:
        raise RuntimeError("Need to supply lame paramters")
    else:
        mu = mu = problem_parameters.get("mu")

    if problem_parameters.get("lambda") is None:
        raise RuntimeError("Need to supply lame paramters")
    else:
        lmbda = problem_parameters.get("lambda")
    if problem_parameters.get("theta") is None:
        raise RuntimeError("Need to supply theta for Nitsche's method")
    else:
        theta = problem_parameters["theta"]
    if problem_parameters.get("gamma") is None:
        raise RuntimeError("Need to supply gamma for Nitsche's method")
    else:
        gamma = problem_parameters.get("gamma")

    # Contact data
    surface_pairs = surface_data[1]
    surfaces = surface_data[0]

    # Mesh, function space and FEM functions
    V = u.function_space
    mesh = V.mesh
    h = ufl.CellDiameter(mesh)

    # Custom assembly
    # create contact class
    with _common.Timer("~Contact " + timing_str + ": Init"):
        contact = dolfinx_contact.cpp.Contact(markers[1:], surfaces, surface_pairs,
                                              V._cpp_object, quadrature_degree=quadrature_degree)
    with _common.Timer("~Contact " + timing_str + ": Distance maps"):
        for i in range(len(surface_pairs)):
            contact.create_distance_map(i)
    # pack constants
    consts = np.array([gamma, theta])

    # Pack material parameters mu and lambda on each contact surface
    with _common.Timer("~Contact " + timing_str + ": Interpolate coeffs (mu, lmbda)"):
        V2 = _fem.FunctionSpace(mesh, ("DG", 0))
        lmbda2 = _fem.Function(V2)
        lmbda2.interpolate(lambda x: np.full((1, x.shape[1]), lmbda))
        mu2 = _fem.Function(V2)
        mu2.interpolate(lambda x: np.full((1, x.shape[1]), mu))

    entities = []
    with _common.Timer("~Contact " + timing_str + ": Compute active entities"):
        for pair in surface_pairs:
            entities.append(contact.active_entities(pair[0]))

    material = []
    with _common.Timer("~Contact " + timing_str + ": Pack coeffs (mu, lmbda"):
        for i in range(len(surface_pairs)):
            material.append(np.hstack([dolfinx_contact.cpp.pack_coefficient_quadrature(
                mu2._cpp_object, 0, entities[i]),
                dolfinx_contact.cpp.pack_coefficient_quadrature(
                lmbda2._cpp_object, 0, entities[i])]))

    # Pack celldiameter on each surface
    h_packed = []
    with _common.Timer("~Contact " + timing_str + ": Compute and pack celldiameter"):
        surface_cells = np.unique(np.hstack([entities[i][:, 0] for i in range(len(surface_pairs))]))
        h_int = _fem.Function(V2)
        expr = _fem.Expression(h, V2.element.interpolation_points())
        h_int.interpolate(expr, surface_cells)
        for i in range(len(surface_pairs)):
            h_packed.append(dolfinx_contact.cpp.pack_coefficient_quadrature(
                h_int._cpp_object, 0, entities[i]))

    # Pack gap, normals and test functions on each surface
    gaps = []
    test_fns = []
    grad_test_fns = []
    with _common.Timer("~Contact " + timing_str + ": Pack gap, normals, testfunction"):
        for i in range(len(surface_pairs)):
            gaps.append(contact.pack_gap(i))
            test_fns.append(contact.pack_test_functions(i))
            grad_test_fns.append(contact.pack_grad_test_functions(i, gaps[i], np.zeros(gaps[i].shape)))

    # Concatenate all coeffs
    coeffs_const = []
    for i in range(len(surface_pairs)):
        coeffs_const.append(np.hstack([material[i], h_packed[i], test_fns[i], grad_test_fns[i]]))

    # Generate Jacobian data structures
    J_custom = _fem.form(lhs, form_compiler_options=form_compiler_options, jit_options=jit_options)
    with _common.Timer("~Contact " + timing_str + ": Generate Jacobian kernel"):
        kernel_jac = contact.generate_kernel(kt.MeshTieJac)
    with _common.Timer("~Contact " + timing_str + ": Create matrix"):
        A = contact.create_matrix(J_custom)

    # Generate residual data structures
    F_custom = _fem.form(rhs, form_compiler_options=form_compiler_options, jit_options=jit_options)
    with _common.Timer("~Contact " + timing_str + ": Generate residual kernel"):
        kernel_rhs = contact.generate_kernel(kt.MeshTieRhs)
    with _common.Timer("~Contact " + timing_str + ": Create vector"):
        b = _fem.petsc.create_vector(F_custom)

    # Compute u dependent coeficcients
    u_candidate = []
    grad_u_candidate = []
    coeffs = []
    with _common.Timer("~~Contact " + timing_str + ": Pack u contact"):
        for i in range(len(surface_pairs)):
            u_candidate.append(contact.pack_u_contact(i, u._cpp_object))
            grad_u_candidate.append(contact.pack_grad_u_contact(i, u._cpp_object, gaps[i], np.zeros(gaps[i].shape)))
    u_puppet = []
    grad_u_puppet = []
    with _common.Timer("~~Contact " + timing_str + ": Pack u"):
        for i in range(len(surface_pairs)):
            u_puppet.append(dolfinx_contact.cpp.pack_coefficient_quadrature(
                u._cpp_object, quadrature_degree, entities[i]))
            grad_u_puppet.append(dolfinx_contact.cpp.pack_gradient_quadrature(
                u._cpp_object, quadrature_degree, entities[i]))
    for i in range(len(surface_pairs)):
        coeffs.append(np.hstack([coeffs_const[i], u_puppet[i], grad_u_puppet[i], u_candidate[i], grad_u_candidate[i]]))

    # Assemble residual vector
    b.zeroEntries()
    with _common.Timer("~~Contact " + timing_str + ": Contact contributions (in assemble vector)"):
        for i in range(len(surface_pairs)):
            contact.assemble_vector(b, i, kernel_rhs, coeffs[i], consts)
    with _common.Timer("~~Contact " + timing_str + ": Pack coefficients ufl"):
        coeffs_ufl = _cppfem.pack_coefficients(F_custom)
    with _common.Timer("~~Contact " + timing_str + ": Pack constants ufl"):
        consts_ufl = _cppfem.pack_constants(F_custom)
    with _common.Timer("~~Contact " + timing_str + ": Standard contributions (in assemble vector)"):
        _fem.petsc.assemble_vector(b, F_custom, constants=consts_ufl, coeffs=coeffs_ufl)  # type: ignore

    # Apply boundary condition
    if len(bcs) > 0:
        x = u.vector
        _fem.petsc.apply_lifting(b, [J_custom], bcs=[bcs], x0=[x], scale=-1.0)
        b.ghostUpdate(addv=_PETSc.InsertMode.ADD, mode=_PETSc.ScatterMode.REVERSE)
        _fem.petsc.set_bc(b, bcs, x, -1.0)

    #  Compute Jacobi Matrix
    A.zeroEntries()
    with _common.Timer("~~Contact " + timing_str + ": Contact contributions (in assemble matrix)"):
        for i in range(len(surface_pairs)):
            contact.assemble_matrix(A, [], i, kernel_jac, coeffs[i], consts)
    with _common.Timer("~~Contact " + timing_str + ": Pack coefficients ufl"):
        coeffs_ufl = _cppfem.pack_coefficients(J_custom)
    with _common.Timer("~~Contact " + timing_str + ": Pack constants ufl"):
        consts_ufl = _cppfem.pack_constants(J_custom)
    with _common.Timer("~~Contact " + timing_str + ": Standard contributions (in assemble matrix)"):
        _fem.petsc.assemble_matrix(A, J_custom, constants=consts_ufl, coeffs=coeffs_ufl, bcs=bcs)  # type: ignore
    A.assemble()

    # Set rigid motion nullspace
    null_space = rigid_motions_nullspace_subdomains(V, markers[0], np.unique(markers[0].values))
    A.setNearNullSpace(null_space)
    # Create PETSc Krylov solver and turn convergence monitoring on
    opts = _PETSc.Options()
    for key in petsc_options:
        opts[key] = petsc_options[key]
    solver = _PETSc.KSP().create(mesh.comm)
    solver.setFromOptions()

    # Set matrix operator
    solver.setOperators(A)

    uh = _fem.Function(V)

    dofs_global = V.dofmap.index_map_bs * V.dofmap.index_map.size_global
    _log.set_log_level(_log.LogLevel.OFF)
    # Set a monitor, solve linear system, and display the solver
    # configuration
    solver.setMonitor(lambda _, its, rnorm: print(f"Iteration: {its}, rel. residual: {rnorm}"))
    timing_str = "~Contact " + timing_str + ": Krylov Solver"
    with _common.Timer(timing_str):
        solver.solve(b, uh.vector)

    # Scatter forward the solution vector to update ghost values
    uh.x.scatter_forward()

    solver_time = _common.timing(timing_str)[1]
    print(f"{dofs_global}\n",
          f"Number of Krylov iterations {solver.getIterationNumber()}\n",
          f"Solver time {solver_time}", flush=True)
    return uh, solver.getIterationNumber(), solver_time, dofs_global
