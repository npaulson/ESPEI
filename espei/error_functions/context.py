"""Convenience function to create a context for the built in error functions"""

import logging
import copy
import sympy
from pycalphad import variables as v
from pycalphad.codegen.callables import build_callables
from pycalphad.core.utils import instantiate_models
from espei.error_functions import get_zpf_data, get_thermochemical_data
from espei.utils import database_symbols_to_fit

TRACE = 15


def setup_context(dbf, datasets, symbols_to_fit=None, data_weights=None, make_callables=True):
    """
    Set up a context dictionary for calculating error.

    Parameters
    ----------
    dbf : Database
        A pycalphad Database that will be fit
    datasets : PickleableTinyDB
        A database of single- and multi-phase data to fit
    symbols_to_fit : list of str
        List of symbols in the Database that will be fit. If None (default) are
        passed, then all parameters prefixed with `VV` followed by a number,
        e.g. VV0001 will be fit.

    Returns
    -------

    Notes
    -----
    A copy of the Database is made and used in the context. To commit changes
    back to the original database, the dbf.symbols.update method should be used.
    """
    dbf = copy.deepcopy(dbf)
    comps = sorted([sp for sp in dbf.elements])
    if symbols_to_fit is None:
        symbols_to_fit = database_symbols_to_fit(dbf)
    else:
        symbols_to_fit = sorted(symbols_to_fit)
    data_weights = data_weights if data_weights is not None else {}

    if len(symbols_to_fit) == 0:
        raise ValueError('No degrees of freedom. Database must contain symbols starting with \'V\' or \'VV\', followed by a number.')
    else:
        logging.info('Fitting {} degrees of freedom.'.format(len(symbols_to_fit)))

    for x in symbols_to_fit:
        if isinstance(dbf.symbols[x], sympy.Piecewise):
            logging.debug('Replacing {} in database'.format(x))
            dbf.symbols[x] = dbf.symbols[x].args[0].expr

    # construct the models for each phase, substituting in the SymPy symbol to fit.
    logging.log(TRACE, 'Building phase models (this may take some time)')
    import time
    t1 = time.time()
    phases = sorted(dbf.phases.keys())
    models = instantiate_models(dbf, comps, phases, parameters=dict(zip(symbols_to_fit, [0]*len(symbols_to_fit))))
    if make_callables:
        eq_callables = build_callables(dbf, comps, phases, models, parameter_symbols=symbols_to_fit,
                            output='GM', build_gradients=True, build_hessians=False,
                            additional_statevars={v.N, v.P, v.T})
    else:
        eq_callables = None
    thermochemical_data = get_thermochemical_data(dbf, comps, phases, datasets, weight_dict=data_weights, symbols_to_fit=symbols_to_fit, make_callables=make_callables)
    t2 = time.time()
    logging.log(TRACE, 'Finished building phase models ({:0.2f}s)'.format(t2-t1))

    # context for the log probability function
    # for all cases, parameters argument addressed in MCMC loop
    error_context = {
        'symbols_to_fit': symbols_to_fit,
        'zpf_kwargs': {
            'dbf': dbf, 'phases': phases, 'zpf_data': get_zpf_data(comps, phases, datasets),
            'phase_models': models, 'callables': eq_callables,
            'data_weight': data_weights.get('ZPF', 1.0),
        },
        'thermochemical_kwargs': {
            'dbf': dbf, 'comps': comps, 'thermochemical_data': thermochemical_data,
        },
        'activity_kwargs': {
            'dbf': dbf, 'comps': comps, 'phases': phases, 'datasets': datasets,
            'phase_models': models, 'callables': eq_callables,
            'data_weight': data_weights.get('ACR', 1.0),
        },
    }
    return error_context
