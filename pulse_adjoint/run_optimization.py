#!/usr/bin/env python
# Copyright (C) 2016 Henrik Finsberg
#
# This file is part of PULSE-ADJOINT.
#
# PULSE-ADJOINT is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PULSE-ADJOINT is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with PULSE-ADJOINT. If not, see <http://www.gnu.org/licenses/>.
from dolfinimport import *
from setup_optimization import setup_simulation, logger, MyReducedFunctional, get_measurements
from utils import Text, Object, pformat, print_line, print_head, get_spaces,  UnableToChangePressureExeption, get_simulated_pressure
from forward_runner import ActiveForwardRunner, PassiveForwardRunner
from optimization_targets import *
from numpy_mpi import *
from adjoint_contraction_args import *
from scipy.optimize import minimize as scipy_minimize
from scipy.optimize import minimize_scalar as scipy_minimize_1d
from pa_io import write_opt_results_to_h5, contract_point_exists
from optimal_control import OptimalControl
from lvsolver import SolverDidNotConverge

def get_constant(value_size, value_rank, val):
    if value_size == 1:
        if value_rank == 0:
            c = Constant(val)
        else:
            c = Constant([val])
    else:
        c = Constant([val]*value_size)
    return c
    
def run_unloaded_optimization(params, patient, initial_guess = 30.0):
    from unloading import UnloadedMaterial
  
    pfd = patient.passive_filling_duration
    if patient.mesh_type() == "biv":
        
        pressures = zip(patient.pressure[:pfd], patient.RVP[:pfd])
        volumes = zip(patient.volume[:pfd], patient.RVV[:pfd])
        p_geometry = pressures[0]
        
        
    else:
        pressures = patient.pressure[:pfd]
        volumes = patient.volume[:pfd]
        p_geometry = pressures[0]
        

    h5group = params["h5group"]
    params["Material_parameters"]["a"] = initial_guess
    
    estimator =  UnloadedMaterial(p_geometry, pressures, volumes,
                                  params, **params["Unloading_parameters"])
    

    
    estimator.unload_material(patient)
    params["h5group"] = h5group
    
        
    
    
def run_passive_optimization(params, patient):
    """
    Main function for the passive phase

    :param dict params: adjoin_contraction_parameters
    :param patient: A patient instance
    :type patient: :py:class`patient_data.Patient`

    **Example of usage**::

      # Setup compiler parameters
      setup_general_parameters()
      params = setup_adjoint_contraction_parameter()
      params['phase'] = 'passive_inflation'
      patient = initialize_patient_data(param['Patient_parameters'], False)
      run_passive_optimization(params, patient)


    """
    

    logger.info(Text.blue("\nRun Passive Optimization"))

    #Load patient data, and set up the simulation
    measurements, solver_parameters, pressure, paramvec = setup_simulation(params, patient)

    rd, paramvec = run_passive_optimization_step(params, 
                                                 patient, 
                                                 solver_parameters, 
                                                 measurements, 
                                                 pressure,
                                                 paramvec)

    logger.info("\nSolve optimization problem.......")
    solve_oc_problem(params, rd, paramvec)


def run_passive_optimization_step(params, patient, solver_parameters, measurements, pressure, paramvec):
    """FIXME! briefly describe function

    :param params: 
    :param patient: 
    :param solver_parameters: 
    :param measurements: 
    :param pressure: 
    :param paramvec: 
    :returns: 
    :rtype: 

    """
    
    # Load targets
    optimization_targets, bcs = load_targets(params, solver_parameters, measurements)

           
    #Initialize the solver for the Forward problem
    for_run = PassiveForwardRunner(solver_parameters, 
                                   pressure, 
                                   bcs,
                                   optimization_targets,
                                   params, 
                                   paramvec)


    # Update the weights for the functional
    if params["adaptive_weights"]:
        #Solve the forward problem with guess results (just for printing)
        logger.info(Text.blue("\nForward solution at guess parameters"))
        forward_result, _ = for_run(paramvec, False)
        
        weights = {}
        for k, v in for_run.opt_weights.iteritems():
            weights[k] = v/(10*forward_result["func_value"])
        for_run.opt_weights.update(**weights)
        logger.info("\nUpdate weights for functional")
        logger.info(for_run._print_functional())
    
    # Stop recording
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True

    # Initialize MyReducedFuctional
    rd = MyReducedFunctional(for_run, paramvec,
                             relax = params["passive_relax"],
                             verbose = params["verbose"])
    
    return rd, paramvec


def run_active_optimization(params, patient):
    """FIXME! briefly describe function

    :param params: 
    :param patient: 
    :returns: 
    :rtype: 

    """
    
    
    logger.info(Text.blue("\nRun Active Optimization"))

    #Load patient data, and set up the simulation
    measurements, solver_parameters, pressure, gamma = setup_simulation(params, patient)


    # Loop over contract points
    i = 0
    logger.info("Number of contract points: {}".format(patient.num_contract_points))
       
    while i < patient.num_contract_points:
        params["active_contraction_iteration_number"] = i

        if not contract_point_exists(params):
            
            # Number of times we have interpolated in order
            # to be able to change the pressure
            attempts = 0
            pressure_change = False
            
            while (not pressure_change and attempts < 8):
               
                try:
                    rd, gamma = run_active_optimization_step(params, patient, 
                                                             solver_parameters, 
                                                             measurements,
                                                             pressure, 
                                                             gamma)
                except UnableToChangePressureExeption:
                    logger.info("Unable to change pressure. Exception caught")

                    logger.info("Lets interpolate. Add one extra point")
                    patient.interpolate_data(i+patient.passive_filling_duration-1)

                    # Update the measurements
                    measurements = get_measurements(params, patient)
                    
                    attempts += 1
                    
                else:
                    pressure_change = True

                    # If you want to apply a different initial guess than
                    # the pevious value, assign this now and evaluate.

                    if params["initial_guess"] == "zero" :
                        zero = get_constant(gamma.value_size(), gamma.value_rank(), 0.0)

                        g = Function(gamma.function_space())
                        g.assign(zero)
                        rd(g)
                    elif params["initial_guess"] == "smooth":

                        # We find a constant that represents the previous state

                        if params["gamma_space"] == "regional":

                            # Sum all regional values with weights given by the size of the regions
                            meshvols = [assemble((1.0)*dx(domain=patient.mesh,
                                                          subdomain_data=patient.sfun)(int(r))) \
                                        for r in set(gather_broadcast(patient.sfun.array()))]
                            meshvol = sum(meshvols)
                            g_arr = gather_broadcast(gamma.vector().array())
                            val = sum(np.multiply(g_arr, meshvols))/float(meshvol)
                            c = get_constant(gamma.value_size(), gamma.value_rank(), val)
                            
                        else:

                            # Project the activation parameter onto the real line
                            g_proj  = project(gamma, FunctionSpace(patient.mesh, "R", 0))
                            val = gather_broadcast(g_proj.vector().array())[0]
                            c = get_constant(gamma.value_size(), gamma.value_rank(), val)


                        g = Function(gamma.function_space())
                        g.assign(c)
                        rd(g)
                        
                    logger.info("\nSolve optimization problem.......")
                    solve_oc_problem(params, rd, gamma)
                    adj_reset()
         
            if not pressure_change:
                raise RuntimeError("Unable to increasure")
            
        else:

            # Make sure to do interpolation if that was done earlier
            plv = get_simulated_pressure(params)
            if not plv == measurements["pressure"][i+1]:
                logger.info("Interpolate")
                patient.interpolate_data(i+patient.passive_filling_duration-1)
                measurements = get_measurements(params, patient)
                i -= 1
        i += 1

def run_active_optimization_step(params, patient, solver_parameters, measurements, pressure, gamma):
    """FIXME! briefly describe function

    :param params: 
    :param patient: 
    :param solver_parameters: 
    :param measurements: 
    :param pressure: 
    :param gamma: 
    :returns: 
    :rtype: 

    """
    
    
    #Get initial guess for gamma
    if params["active_contraction_iteration_number"] == 0:
        
        zero = get_constant(gamma.value_size(), gamma.value_rank(), 0.0)
        gamma.assign(zero)


    else:

        # Use gamma from the previous point as initial guess
        # Load gamma from previous point
        g_temp = Function(gamma.function_space())
        with HDF5File(mpi_comm_world(), params["sim_file"], "r") as h5file:
            h5file.read(g_temp, "active_contraction/contract_point_{}/optimal_control".format(params["active_contraction_iteration_number"]-1))
            
        gamma.assign(g_temp)
            
    # Load targets
    optimization_targets, bcs = load_targets(params, solver_parameters, measurements)
   
    for_run = ActiveForwardRunner(solver_parameters,
                                  pressure,
                                  bcs,
                                  optimization_targets,
                                  params,
                                  gamma)
 
    # Update weights so that the initial value of the
    # functional is 0.1
    if params["adaptive_weights"]:
        #Solve the forward problem with guess results (just for printing)
        logger.info(Text.blue("\nForward solution at guess parameters"))
        forward_result, _ = for_run(gamma, False)
        
        weights = {}
        for k, v in for_run.opt_weights.iteritems():
            weights[k] = v/(10*forward_result["func_value"])
        for_run.opt_weights.update(**weights)
        logger.info("Update weights for functional")
        logger.info(for_run._print_functional())
    
    # Stop recording
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True

    rd = MyReducedFunctional(for_run, gamma,
                             relax = params["active_relax"],
                             verbose = params["verbose"])
   
    return rd, gamma

 
    


def store(params, rd, opt_result):

    solver = rd.for_run.cphm.solver
   
    if params["phase"] == PHASES[0]:

        h5group =  "/".join([params["h5group"],PASSIVE_INFLATION_GROUP])
    else:
        
        h5group =  "/".join([params["h5group"],
                            ACTIVE_CONTRACTION_GROUP.format(params["active_contraction_iteration_number"])])
        
    write_opt_results_to_h5(h5group,
                            params,
                            rd.for_res,
                            solver, 
                            opt_result)
    
    
        
def solve_oc_problem(params, rd, paramvec, return_solution = False):
    """Solve the optimal control problem

    :param params: Application parameters
    :param rd: The reduced functional
    :param paramvec: The control parameter(s)

    """

    
    
    opt_params = params["Optimization_parameters"]
    x = gather_broadcast(paramvec.vector().array())
    nvar = len(x)
    
    if params["phase"] == PHASES[0] and not params["optimize_matparams"]:
        
        
        rd(x)
        rd.for_res["initial_control"] = rd.initial_paramvec,
        rd.for_res["optimal_control"] = rd.paramvec
        store(params, rd, {})

    
    else:

        logger.info("\n"+"".center(72,"-"))
        logger.info("Solve optimal contol problem".center(72, "-"))
        logger.info("".center(72,"-"))

        # Some flags
        solved = False
        done = False
        paramvec_start = paramvec.copy(True)
        state_start = rd.for_run.cphm.get_state()
        niter = 0

        par_max = np.max(gather_broadcast(paramvec_start.vector().array()))
        par_min = np.min(gather_broadcast(paramvec_start.vector().array()))
        gamma_max = float(params["Optimization_parameters"]["gamma_max"])
        mat_max = float(params["Optimization_parameters"]["matparams_max"])
        mat_min = float(params["Optimization_parameters"]["matparams_min"])
       
        while not done and niter < 5:
            # Evaluate the reduced functional in case the solver chrashes at the first point.
            # If this is not done, and the solver crashes in the first point
            # then Dolfin adjoit has no recording and will raise an exception.
            
            # If this fails, there is no hope.
            try:
              
                rd(paramvec)
            except SolverDidNotConverge:
                print "NOOOO!"
                if len(rd.controls_lst) > 0:
                    assign_to_vector(paramvec.vector(),
                                     rd.controls_lst[-1].array())
                else:
                    msg = ("Unable to converge. "+
                           "Choose a different initial guess")
                    logger.error(msg)
                try:
                    rd(paramvec)
                except:
                    msg = ("Unable to converge. "+
                           "Try changing the scales and restart")
                    logger.error(msg)
                
            # Create optimal control problem
            oc_problem = OptimalControl()
            oc_problem.build_problem(params, rd, paramvec)
            
            try:
                # Try to solve the problem
                rd, opt_result = oc_problem.solve()
                
            except SolverDidNotConverge:

                logger.warning(Text.red("Solver failed - reduce step size"))
                # If the solver did not converge assign the state from
                # previous iteration and reduce the step size and try again
                rd.reset()
                rd.derivative_scale /= 2.0

                # There might be many reasons for why the sovler is not converging, 
                # but most likely it happens because the optimization algorithms try to
                # evaluate the function in a point in the parameter space, which is close
                # to the boundary. One thing we can do is to reduce the mangnitude of the
                # gradient (but keeping the direction) so that the step size reduces.
                # Another thing we can do is to actually change the bounds so that
                # the algorithm do not go into the nasty parts of the parameters space.
                # Usually the main problem is that the optimziation tries an activation that
                # is too strong (high gamma max) in the active phase, or at material parameter
                # set that is too soft (low material parameters) in the passive phase
                params["Optimization_parameters"]["gamma_max"] = np.max([par_max, 0.9*params["Optimization_parameters"]["gamma_max"]])
                params["Optimization_parameters"]["matparams_min"] = np.min([par_min, 2*params["Optimization_parameters"]["matparams_min"]])
                                
            else:
                params["Optimization_parameters"]["gamma_max"] = gamma_max
                params["Optimization_parameters"]["matparams_min"] = mat_min
               
                done = True
                
            
            niter += 1
                    

        if not done:
            opt_result = {}
            control_idx = np.argmin(rd.func_values_lst)
            x = gather_broadcast(rd.controls_lst[control_idx].array())
            msg = "Unable to solve problem. Choose the best value"
            logger.warning(msg)
        else:
            x = np.array([opt_result.pop("x")]) if nvar == 1 else gather_broadcast(opt_result.pop("x"))
            
        assign_to_vector(paramvec.vector(), gather_broadcast(x))

        
        logger.info(Text.blue("\nForward solution at optimal parameters"))
        val = rd.for_run(paramvec, False)
          
        rd.for_res["initial_control"] = rd.initial_paramvec,
        rd.for_res["optimal_control"] = rd.paramvec
        
        
        print_optimization_report(params, rd.paramvec, rd.initial_paramvec,
                                  rd.ini_for_res, rd.for_res, opt_result)
        
        store(params, rd, opt_result)
        if return_solution:
            return params, rd, opt_result

def print_optimization_report(params, opt_controls, init_controls, 
                              ini_for_res, opt_for_res, opt_result = None):

    from numpy_mpi import gather_broadcast

    if opt_result:
        logger.info("\nOptimization terminated...")
     
        logger.info("\tFunction Evaluations: {}".format(opt_result["nfev"]))
        logger.info("\tGradient Evaluations: {}".format(opt_result["njev"]))
        logger.info("\tNumber of iterations: {}".format(opt_result["nit"]))
        logger.info("\tNumber of crashes: {}".format(opt_result["ncrash"]))
        logger.info("\tRun time: {:.2f} seconds".format(opt_result["run_time"]))

    logger.info("\nFunctional Values")
    logger.info(" "*7+"\t"+print_head(ini_for_res, False))

    if not opt_result.has_key("grad_norm") or len(opt_result["grad_norm"]) == 0:
        grad_norm_ini = 0.0
        grad_norm_opt = 0.0
    else:
        grad_norm_ini = opt_result["grad_norm"][0]
        grad_norm_opt = opt_result["grad_norm"][-1]
        

    logger.info("{:7}\t{}".format("Initial", print_line(ini_for_res, grad_norm=grad_norm_ini)))
    logger.info("{:7}\t{}".format("Optimal", print_line(opt_for_res, grad_norm=grad_norm_opt)))
    
    if params["phase"] == PHASES[0]:
        logger.info("\nMaterial Parameters")
        logger.info("Initial {}".format(init_controls))
        logger.info("Optimal {}".format(gather_broadcast(opt_controls.vector().array())))
    else:
        logger.info("\nContraction Parameter")
        logger.info("\tMin\tMean\tMax")
        logger.info("Initial\t{:.5f}\t{:.5f}\t{:.5f}".format(init_controls.min(), 
                                                             init_controls.mean(), 
                                                             init_controls.max()))
        opt_controls_arr = gather_broadcast(opt_controls.vector().array())
        logger.info("Optimal\t{:.5f}\t{:.5f}\t{:.5f}".format(opt_controls_arr.min(), 
                                                             opt_controls_arr.mean(), 
                                                             opt_controls_arr.max()))


def load_target_data(measurements, params, optimization_targets):
    """Load the target data into dolfin functions.
    The target data will be loaded into the optiization_targets

    :param measurements: The target measurements
    :param params: Application parameters
    :param optimization_targer: A dictionary with the targets
    :returns: object with target data
    :rtype: object
    """

    logger.debug(Text.blue("Loading Target Data"))

    # The point in the acitve phase (0 if passive)
    acin = params["active_contraction_iteration_number"]
    biv = params["Patient_parameters"]["mesh_type"] == "biv"

    # Load boundary conditions
    bcs = {}

    pressure = measurements["pressure"]
    if biv:
        rv_pressure = measurements["rv_pressure"]
        
    
    if params["phase"] == PHASES[1]:
        pressure = pressure[acin: 2 + acin]
        if biv:
            rv_pressure = rv_pressure[acin: 2 + acin]
        
    bcs["pressure"] = pressure
    if biv:
        bcs["rv_pressure"] = rv_pressure
    


    # Load the target data into dofin functions
    for key, val in params["Optimization_targets"].iteritems():

        # If target is included in the optimization
        if val:
            # Load the target data
            for it,p in enumerate(pressure):
                optimization_targets[key].load_target_data(measurements[key], it+acin)
                
    return optimization_targets, bcs


def get_optimization_targets(params, solver_parameters):
    """FIXME! briefly describe function

    :param params: 
    :param solver_parameters: 
    :returns: 
    :rtype: 

    """
    

    p = params["Optimization_targets"]
    mesh = solver_parameters["mesh"]
    if params["phase"] == PHASES[0]:
        reg_par = params["Passive_optimization_weigths"]["regularization"]
        spacestr = params["matparams_space"]
    else:
        reg_par = params["Active_optimization_weigths"]["regularization"]
        spacestr = params["gamma_space"]

    

    targets = {"regularization": Regularization(mesh,spacestr, reg_par)}

    if p["volume"]:
        
        if solver_parameters["markers"].has_key("ENDO_LV"):
            marker = "ENDO_LV"
        else:
            marker = "ENDO"
            
        dS = Measure("exterior_facet",
                     subdomain_data = solver_parameters["facet_function"],
                     domain = mesh)(solver_parameters["markers"][marker][0])
        
        targets["volume"] = VolumeTarget(mesh,dS, "LV", approx = params["volume_approx"])

    if p["rv_volume"]:
            
        dS = Measure("exterior_facet",
                     subdomain_data = solver_parameters["facet_function"],
                     domain = mesh)(solver_parameters["markers"]["ENDO_RV"][0])
        
        targets["rv_volume"] = VolumeTarget(mesh,dS, "RV", approx = params["volume_approx"])

    if p["regional_strain"]:

        dX = Measure("dx",
                     subdomain_data = solver_parameters["mesh_function"],
                     domain = mesh)


   
        load_displacemet = (params["unload"] and not params["strain_reference"] == "unloaded") or \
                           (not params["unload"] and params["strain_reference"] == "ED")
        
        if load_displacemet and params["phase"] == PHASES[1]:
            # We need to recompute strains wrt reference as diastasis
         
            

            if params["strain_reference"] == "0":
                group = "1"
            else:
                #strain reference =  "ED"

                if params["unload"]:
                    group = str(solver_parameters["passive_filling_duration"])
                else:
                    group = str(solver_parameters["passive_filling_duration"]-1)

            family, degree = solver_parameters["state_space"].split(":")[0].split("_")
            u = Function(VectorFunctionSpace(solver_parameters["mesh"], family, int(degree)))
            
            with HDF5File(mpi_comm_world(), params["sim_file"], 'r') as h5file:
        
                # Get previous state
                group = "/".join([params["h5group"],
                                  PASSIVE_INFLATION_GROUP,
                                  "displacement",group])
                h5file.read(u, group)

            
            if params["strain_approx"] in ["project","interpolate"]:

                V = VectorFunctionSpace(solver_parameters["mesh"], "CG", 1)
                if params["strain_approx"] == "project":
                    u = project(u, V)
                else:
                    u = interpolate(u, V)
                    
                
            F_ref = grad(u) + Identity(3)
                

        else:
            F_ref = Identity(3)

        targets["regional_strain"] = \
            RegionalStrainTarget(mesh,
                                 solver_parameters["crl_basis"],
                                 dX,
                                 solver_parameters["strain_weights"],
                                 F_ref = F_ref,
                                 approx = params["strain_approx"])
    
        

    return targets
        
    
def load_targets(params, solver_parameters, measurements):
    """FIXME! briefly describe function

    :param dict params: 
    :param dict solver_parameters: 
    :param dict measurements: 
    :returns: A tuple containing 1. a dictionary with
              optimization targets and 2. boundary conditions
    :rtype: tuple

    """
    
    
    #Solve calls are not registred by libajoint
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True

    
    # Load optimization targets
    optimization_targets = get_optimization_targets(params, solver_parameters)

    # Load target data
    optimization_targets, bcs = \
        load_target_data(measurements, params, optimization_targets)

    
    
    
    # Start recording for dolfin adjoint 
    logger.debug(Text.yellow("Start annotating"))
    parameters["adjoint"]["stop_annotating"] = False

    return optimization_targets, bcs
