#!/usr/bin/env python
# Copyright (C) 2016 Henrik Finsberg
#
# This file is part of CAMPASS.
#
# CAMPASS is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CAMPASS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with CAMPASS. If not, see <http://www.gnu.org/licenses/>.
from dolfinimport import *
from compressibility import get_compressibility
from adjoint_contraction_args import logger
from copy import deepcopy

class LVSolver(object):
    """
    A Cardiac Mechanics Solver
    """
    
    def __init__(self, params):        

        for k in ["mesh", "facet_function", "material", "bc"]:
            assert params.has_key(k), \
              "{} need to be in solver_parameters".format(k)

        
        self.parameters = params

        # Krylov solvers does not work
        self.iterative_solver = False

        # Update solver parameters
        if params.has_key("solve"):
            if params["solve"].has_key("nonlinear_solver") \
              and params["solve"]["nonlinear_solver"] == "newton":
                self.use_snes == False
            else:
                self.use_snes = True
              
            prm = self.default_solver_parameters()

            for k, v in params["solve"].iteritems():
                if isinstance(params["solve"][k], dict):
                    for k_sub, v_sub in params["solve"][k].iteritems():
                        prm[k][k_sub]= v_sub

                else:
                    prm[k] = v
        else:
            prm = self.default_solver_parameters()
            
        self.parameters["solve"] = prm
        
        
        self._init_spaces()
        self._init_forms()

    def default_solver_parameters(self):

        nsolver = "snes_solver" if self.use_snes else "newton_solver"

        prm = {"nonlinear_solver": "snes", "snes_solver":{}} if self.use_snes else {"nonlinear_solver": "newton", "newton_solver":{}}

        prm[nsolver]['absolute_tolerance'] = 1E-5
        prm[nsolver]['relative_tolerance'] = 1E-5
        prm[nsolver]['maximum_iterations'] = 8
        # prm[nsolver]['relaxation_parameter'] = 1.0
        prm[nsolver]['linear_solver'] = 'lu'
        prm[nsolver]['error_on_nonconvergence'] = True
        prm[nsolver]['report'] = True if logger.level < INFO else False
        if self.iterative_solver:
            prm[nsolver]['linear_solver'] = 'gmres'
            prm[nsolver]['preconditioner'] = 'ilu'

            prm[nsolver]['krylov_solver'] = {}
            prm[nsolver]['krylov_solver']['absolute_tolerance'] = 1E-9
            prm[nsolver]['krylov_solver']['relative_tolerance'] = 1E-7
            prm[nsolver]['krylov_solver']['maximum_iterations'] = 1000
            prm[nsolver]['krylov_solver']['monitor_convergence'] = False
            prm[nsolver]['krylov_solver']['nonzero_initial_guess'] = False

            prm[nsolver]['krylov_solver']['gmres'] = {}
            prm[nsolver]['krylov_solver']['gmres']['restart'] = 40

            prm[nsolver]['krylov_solver']['preconditioner'] = {}
            prm[nsolver]['krylov_solver']['preconditioner']['structure'] = 'same_nonzero_pattern'

            prm[nsolver]['krylov_solver']['preconditioner']['ilu'] = {}
            prm[nsolver]['krylov_solver']['preconditioner']['ilu']['fill_level'] = 0

        return prm
           
        
    def get_displacement(self, name = "displacement", annotate = True):
        return self._compressibility.get_displacement(name, annotate)
    def get_u(self):
        if self._W.sub(0).num_sub_spaces() == 0:
            return self._w
        else:
            return split(self._w)[0]

    def get_gamma(self):
        return self.parameters["material"].gamma

    def is_incompressible(self):
        return self._compressibility.is_incompressible()

    def get_state(self):
        return self._w

    def get_state_space(self):
        return self._W
    
    def reinit(self, w):
        """
        *Arguments*
          w (:py:class:`dolfin.GenericFunction`)
            The state you want to assign

        Assign given state, and reinitialize variaional form.
        """
        self.get_state().assign(w, annotate=False)
        self._init_forms()
    

    def solve(self):
        r"""
        Solve the variational problem

        .. math::

           \delta W = 0

        """
        # Get old state in case of non-convergence
        w_old = self.get_state().copy(True)
        try:
            # Try to solve the system
             solve(self._G == 0,
                   self._w,
                   self._bcs,
                   J = self._dG,
                   solver_parameters = self.parameters["solve"],
                   annotate = False)

        except RuntimeError:
            # Solver did not converge
            logger.warning("Solver did not converge")
            # Retrun the old state, and a flag crash = True
            self.reinit(w_old)
            return w_old, True

        else:
            # The solver converged
            
            # If we are annotating we need to annotate the solve as well
            if not parameters["adjoint"]["stop_annotating"]:

                # Assign the old state
                self._w.assign(w_old)
                # Solve the system with annotation
                solve(self._G == 0,
                      self._w,
                      self._bcs,
                      J = self._dG,
                      solver_parameters = self.parameters["solve"], 
                      annotate = True)

            # Return the new state, crash = False
            return self._w, False

    def internal_energy(self):
        """
        Return the total internal energy
        """
        return self._pi_int

    def first_piola_stress(self):
        r"""
        First Piola Stress Tensor

        Incompressible:

        .. math::

           \mathbf{P} =  \frac{\partial \psi}{\partial \mathbf{F}} - p\mathbf{F}^T

        Compressible:

        .. math::

           \mathbf{P} = \frac{\partial \psi}{\partial \mathbf{F}}
        
        """

        if self.is_incompressible():
            p = self._compressibility.p
            return diff(self._strain_energy, self._F) - p*self._F.T
        else:
            return diff(self._strain_energy, self._F)

    def second_piola_stress(self):
        r"""
        Second Piola Stress Tensor

        .. math::

           \mathbf{S} =  \mathbf{F}^{-1} \mathbf{P}

        """
        return inv(self._F)*self.first_piola_stress()

    def chaucy_stress(self):
        r"""
        Chaucy Stress Tensor

        Incompressible:

        .. math::

           \sigma = \mathbf{F} \frac{\partial \psi}{\partial \mathbf{F}} - pI

        Compressible:

        .. math::

           \sigma = \mathbf{F} \frac{\partial \psi}{\partial \mathbf{F}}
        
        """
        if self.is_incompressible():
            p = self._compressibility.p
            return self._F*diff(self._strain_energy, self._F) - p*self._I 
        else:
            J = det(self._F)
            return J**(-1)*self._F*diff(self._strain_energy, self._F)

    def fiber_stress(self):
        r"""Compute Fiber stress

        .. math::

           \sigma_{f} = f \cdot \sigma f,

        with :math:`\sigma` being the Chauchy stress tensor
        and :math:`f` the fiber field on the current configuration

        """
        # Fibers
        f0 = self.parameters["material"].f0
        f = self._F*f0

        return inner(f, self.chaucy_stress()*f)

    def fiber_strain(self):
        r"""Compute Fiber strain

        .. math::

           \mathbf{E}_{f} = f \cdot \mathbf{E} f,

        with :math:`\mathbf{E}` being the Green-Lagrange strain tensor
        and :math:`f` the fiber field on the current configuration

        """
        # Fibers
        f0 = self.parameters["material"].f0
        f = self._F*f0

        return inner(f, self._E*f)

    def work(self):
        r"""
        Compute Work

        .. math::

           W = \mathbf{S} : \mathbf{E},

        with :math:`\mathbf{E}` being the Green-Lagrange strain tensor
        and :math:`\mathbf{E}` the second Piola stress tensor
        """
        return inner(self._E, self.second_piola_stress())
        
    def work_fiber(self):
        r"""Compute Work in Fiber work

        .. math::

           W = \mathbf{S}_f : \mathbf{E}_f,
        """
        
        # Fibers
        f0 = self.parameters["material"].f0
        f = self._F*f0

        Ef = self._E*f
        Sf = self.second_piola_stress()*f
        
        return inner(Ef, Sf)

    def I1(self):
        """
        Return first isotropic invariant
        """
        return self.parameters["material"].I1(self._F)

    def I4f(self):
        """
        Return the quasi-invariant in fiber direction
        """
        return self.parameters["material"].I4f(self._F)

    def _init_spaces(self):
        """
        Initialize function spaces
        """
        
        self._compressibility = get_compressibility(self.parameters)
            
        self._W = self._compressibility.W
        self._w = self._compressibility.w
        self._w_test = self._compressibility.w_test


    def _init_forms(self):
        r"""
        Initialize variational form

        """
        material = self.parameters["material"]
        N =  self.parameters["facet_normal"]
        ds = Measure("exterior_facet", subdomain_data \
                     = self.parameters["facet_function"])

        # Displacement
        u = self._compressibility.get_displacement_variable()
        self._I = Identity(self.parameters["mesh"].topology().dim())
        # Deformation gradient
        F = grad(u) + self._I
        self._F = variable(F)
        J = det(self._F)
        self._C = F.T*F
        self._E = 0.5*(self._C - self._I)
        
        # Internal energy
        self._strain_energy = material.strain_energy(F)
        self._pi_int = self._strain_energy + self._compressibility(J)      
        # Internal virtual work
        self._G = derivative(self._pi_int*dx, self._w, self._w_test)

        # External work
        v = self._compressibility.u_test

        # Neumann BC
        if self.parameters["bc"].has_key("neumann"):
            for neumann_bc in self.parameters["bc"]["neumann"]:
                p, marker = neumann_bc
                self._G += inner(J*p*dot(inv(F).T, N), v)*ds(marker)
        
        # Robin BC
        if self.parameters["bc"].has_key("robin"):
            for robin_bc in self.parameters["bc"]["robin"]:
                val, marker = robin_bc
                self._G += -inner(val*u, v)*ds(marker)
        
        # Other body forces
        if self.parameters.has_key("body_force"):
            self._G += -inner(self.parameters["body_force"], v)*dx

        # Dirichlet BC
        if self.parameters["bc"].has_key("dirichlet"):
            if hasattr(self.parameters["bc"]["dirichlet"], '__call__'):
                self._bcs = self.parameters["bc"]["dirichlet"](self._W)
            else:
                self._bcs = self._make_dirichlet_bcs()

        
        self._dG = derivative(self._G, self._w)

    def _make_dirichlet_bcs(self):
        bcs = []
        D = self._compressibility.get_displacement_space()
        for bc_spec in self.parameters["bc"]["dirichlet"]:
            val, marker = bc_spec
            if type(marker) == int:
                args = [D, val, self.parameters["facet_function"], marker]
            else:
                args = [D, val, marker]
            bcs.append(DirichletBC(*args))
        return bcs 


