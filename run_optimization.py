from dolfin import *
from dolfin_adjoint import *
from setup_optimization import setup_simulation, ActiveReducedFunctional, logger
from utils import Text, Object, pformat, print_optimization_report, contract_point_exists, get_spaces
from forward_runner import ActiveForwardRunner, PassiveForwardRunner
from heart_problem import PassiveHeartProblem
from numpy_mpi import *
from adjoint_contraction_args import *
from scipy.optimize import minimize as scipy_minimize
from store_opt_results import write_opt_results_to_h5

def run_passive_optimization(params, patient):

    logger.info(Text.blue("\nRun Passive Optimization"))

    #Load patient data, and set up the simulation
    measurements, solver_parameters, p_lv, paramvec = setup_simulation(params, patient)

    controls, rd, for_run, forward_result = \
      run_passive_optimization_step(params, 
                                    patient, 
                                    solver_parameters, 
                                    measurements, 
                                    p_lv, paramvec)

    solve_passive_oc_problem(rd, params, paramvec, for_run, forward_result)


def run_passive_optimization_step(params, patient, solver_parameters, measurements, p_lv, paramvec):
    

    

    mesh = solver_parameters["mesh"]
    spaces = get_spaces(mesh)
    crl_basis = (patient.e_circ, patient.e_rad, patient.e_long)
     
    
    #Solve calls are not registred by libajoint
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True

    #Initialize variables used in the simulation
    phm = PassiveHeartProblem(measurements.pressure, 
                              solver_parameters, 
                              p_lv, patient.ENDO, 
                              crl_basis, spaces)

    
    # Load target data
    target_data = load_target_data(measurements, params, spaces)

    adj_reset()
    # Start recording for dolfin adjoint 
    logger.debug(Text.yellow("Start annotating"))
    parameters["adjoint"]["stop_annotating"] = False

       
    logger.debug(Text.blue("\nCreating Recording for Dolfin-adjoint"))
    #Initialize the solver for the Forward problem
    for_run = PassiveForwardRunner(solver_parameters, 
                                   p_lv, 
                                   target_data,  
                                   patient.ENDO,
                                   crl_basis,
                                   params, 
                                   spaces)

    #Assign guess parameters
    paramvec.assign(Constant(params["Material_parameters"].values()))
    
    
    #Solve the forward problem
    forward_result = for_run(paramvec, True)
    
    
    # Define functional that we want to minimize
    I = Functional(forward_result.total_functional)
    # Set parameters that you want to minimize the functional with respect to
    controls = Control(paramvec)

    # Stop recording
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True
    # Compute functional as a function of the control parameters only
    rd = ReducedFunctional(I, controls)


    return controls, rd, for_run, forward_result
    
    
    # if params["mode"] == MODES[0]:
    #     # Test that the functional has the correct value
    #     test_functional(rd, paramvec, for_run, args)
    
    # elif params["mode"] == MODES[1]: 
    #     # Test that the gradient is computed correctly
    #     run_taylor_test(rd, controls, forward_res.func_value)


        
        
        
    # elif params["mode"] == MODES[3]:
    #     tol = 1e-12
    #     assert replay_dolfin(tol=tol), "replay test fail with tolerance {}".format(tol)
    #     #mpi_print(replay_dolfin(tol=1e-12))

def run_active_optimization(params, patient):
    
    logger.info(Text.blue("\nRun Active Optimization"))

    #Load patient data, and set up the simulation
    measurements, solver_parameters, p_lv, gamma = setup_simulation(params, patient)
    
    # Loop over contract points
    for i in range(patient.num_contract_points):
        params["active_contraction_iteration_number"] = i
        if not contract_point_exists(params):

            rd, gamma = run_active_optimization_step(params, patient, 
                                                     solver_parameters, 
                                                     measurements, p_lv, 
                                                     gamma)


            logger.info("\nSolve optimization problem.......")
            solve_active_oc_problem(params, rd, gamma)

def run_active_optimization_step(params, patient, solver_parameters, measurements, p_lv, gamma):

    
    # Circumferential, radial and logitudinal basis vectors
    crl_basis = (patient.e_circ, patient.e_rad, patient.e_long)
    mesh = solver_parameters["mesh"]

    # Initialize spaces
    spaces = get_spaces(mesh)
    

    #Get previous gamma
    if params["active_contraction_iteration_number"] == 0:
        # Start with 0
        gamma.assign(Constant(0.0))
    else:
        # Load gamma from previous point
        with HDF5File(mpi_comm_world(), params["sim_file"], "r") as h5file:
            h5file.read(gamma, "alpha_{}/active_contraction/contract_point_{}/parameters/activation_parameter_function/".format(params["alpha"], params["active_contraction_iteration_number"]-1))
        

    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True
    
    target_data = load_target_data(measurements, params, spaces)

    logger.debug(Text.yellow("Start annotating"))
    parameters["adjoint"]["stop_annotating"] = False
   
    for_run = ActiveForwardRunner(solver_parameters,
                                  p_lv,
                                  target_data,
                                  params,
                                  gamma, 
                                  patient, 
                                  spaces)


    # Stop recording
    logger.debug(Text.yellow("Stop annotating"))
    parameters["adjoint"]["stop_annotating"] = True

    # Compute the functional as a pure function of gamma
    rd = ActiveReducedFunctional(for_run, gamma)
            
    return rd, gamma

 
    
def solve_active_oc_problem(params, rd, gamma):
    
    paramvec_arr = gather_broadcast(gamma.vector().array())
    
    kwargs = {"method": params["Optimization_parmeteres"]["method"],
              "jac": rd.derivative,
              "tol": params["Optimization_parmeteres"]["active_opt_tol"],
              "bounds": [(0.0, params["Optimization_parmeteres"]["gamma_max"])]*len(paramvec_arr),
              "options": {"disp": params["Optimization_parmeteres"]["disp"], 
                          "maxiter":params["Optimization_parmeteres"]["active_maxiter"]}}

    # Solve the optimization problem
    opt_result = scipy_minimize(rd,paramvec_arr, **kwargs)
    assign_to_vector(gamma.vector(), opt_result.x)

    print_optimization_report(params, rd.gamma, rd.ini_for_res, rd.for_res, opt_result)

    if params["store"]:
        # Store results in .h5 format
        h5group =  ACTIVE_CONTRACTION_GROUP.format(params["alpha"], params["active_contraction_iteration_number"])
        
        # Write results
        write_opt_results_to_h5(h5group, params, rd.ini_for_res, rd.for_res, 
                                opt_gamma = gamma, opt_result = opt_result)

def solve_passive_oc_problem(rd, params, paramvec, forward_runner, ini_for_res):
    if params["optimize_matparams"]:
        # Set upper bound on the parameters
        max_params = Function(paramvec)
        max_params.assign(Constant([50.0]*4))
        # Set lower bound on the parameters
        min_params = Function(paramvec)
        min_params.assign(Constant([0.01]*4))

        # Solve the optimization problem
        opt_controls = minimize(rd,
                                method = OPTIMIZATION_METHOD,
                                options= {"maxiter": params["Optimization_parmeteres"]["passive_maxiter"], 
                                          "disp": True},#params["Optimization_parmeteres"]["disp"]},  
                                bounds = [[min_params], [max_params]],
                                tol =  params["Optimization_parmeteres"]["passive_opt_tol"],
                                scale = params["Optimization_parmeteres"]["scale"])

        # paramvec_arr = gather_broadcast(paramvec.vector().array())
    
        # kwargs = {"method": params["Optimization_parmeteres"]["method"],
        #           "jac": rd.derivative,
        #           "tol": params["Optimization_parmeteres"]["passive_opt_tol"],
        #           "bounds": [(0.1, 50.0)]*len(paramvec_arr),
        #           "options": {"disp": params["Optimization_parmeteres"]["disp"], 
        #                       "maxiter":params["Optimization_parmeteres"]["passive_maxiter"]}}

        # # Solve the optimization problem
        # opt_result = scipy_minimize(rd, paramvec_arr, **kwargs)
        # assign_to_vector(parmvec.vector(), opt_result.x)


    else:
        opt_controls = paramvec
        
    opt_result = None
        

    # Solve the forward problem with optimal parameters
    for_result_opt = forward_runner(opt_controls)
    print_optimization_report(params, opt_controls, ini_for_res, for_result_opt, opt_result)
    
    if params["store"]:
        
        h5group =  PASSIVE_INFLATION_GROUP.format(params["alpha_matparams"])
        
        write_opt_results_to_h5(h5group, params, ini_for_res, for_result_opt, 
                                opt_matparams = opt_controls)






def load_target_data(measurements, params, spaces):
    logger.debug(Text.blue("Loading Target Data"))
        
    def get_strain(newfunc, i, it):
        assign_to_vector(newfunc.vector(), np.array(measurements.strain[i][it]))


    def get_volume(newvol, it):
        assign_to_vector(newvol.vector(), np.array([measurements.volume[it]]))

    # The target data is put into functions so that Dolfin-adjoint can properly record it.
 
    # Store target strains and volumes
    target_strains = []
    target_vols = []

    acin = params["active_contraction_iteration_number"]

    if params["phase"] == PHASES[0]:
        pressures = measurements.pressure
    else:
        pressures = measurements.pressure[acin: 2 + acin]
       

    logger.info(Text.blue("Load target data"))
    logger.info("\tLV Pressure (cPa) \tLV Volume (mL)")


    for it, p in enumerate(pressures):
        
        if params["use_deintegrated_strains"]:
            newfunc = Function(spaces.strainfieldspace, name = \
                               "strain_{}".format(args.active_contraction_iteration_number+it))
          
            assign_to_vector(newfunc.vector(), \
                             gather_broadcast(measurements.strain_deintegrated[acin+it].array()))
            
            target_strains.append(newfunc)
            
        else:
            strains_at_pressure = []
            for i in STRAIN_REGION_NUMS:
                newfunc = Function(spaces.strainspace, name = "strain_{}_{}".format(acin+it, i))
                get_strain(newfunc, i, acin+it)
                strains_at_pressure.append(newfunc)

            target_strains.append(strains_at_pressure)

        newvol = Function(spaces.r_space, name = "newvol")
        get_volume(newvol, acin+it)
        target_vols.append(newvol)

        logger.info("\t{:.3f} \t\t\t{:.3f}".format(p,gather_broadcast(target_vols[-1].vector().array())[0]))



    target_data = Object()
    target_data.target_strains = target_strains
    target_data.target_vols = target_vols
    target_data.target_pressure = pressures


    return target_data