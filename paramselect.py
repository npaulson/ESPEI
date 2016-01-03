"""
The paramselect module handles automated parameter selection for linear models.


Automated Parameter Selection
End-members

Note: All magnetic parameters from literature for now.
Note: No fitting below 298 K (so neglect third law issues for now).
Note: Should use calculate(..., mode='numpy') for this step for performance reasons.

For each step, add one parameter at a time and compute AIC with max likelihood.

Cp - TlnT, T**2, T**-1, T**3 - 4 candidate models
(S and H only have one required parameter each -- will fit in full MCMC procedure)

Choose parameter set with best AIC score.

4. G (full MCMC) - all parameters selected at least once by above procedure

Choice of priors:

(const.): Normal(mean=0, sd=1e6)
T:        Normal(mean=0, sd=50)
TlnT:     Normal(mean=0, sd=20)
T**2:     Normal(mean=0, sd=10)
T**-1:    Normal(mean=0, sd=1e6)
T**3:     Normal(mean=0, sd=5)

Should we use the ridge regression method instead of MCMC for selection?
If we use zero-mean normally-distributed priors, we just construct
a diagonal weighting matrix like 1/variance, and it's actually
equivalent. (But much, much faster to calculate.)
But then how do we compute a model score?
We compute AIC from 2k - 2ln(L_max), where L_max is max likelihood and k is
the number of parameters. All we need, then, is our likelihood estimate.
How do we get that from the regression result?
1. Use lstsq (or ridge) to get parameter values at max likelihood (min error).
2. Plug parameter values into likelihood function to get L_max.
3. Compute AIC.
4. Choose model with minimum AIC.

Looks straightforward: see Tikhonov regularization on Wikipedia
I think we're okay on parameter correlations if we build the matrix correctly

If we do pinv method like ZKL suggested, it's like doing Bayesian regression
with uninformative priors. With bias of AIC toward complex models, I think doing regularization
with ridge regression is advisible.
"""
import pycalphad.variables as v
from pycalphad import calculate, Database, Model
import pycalphad.refstates
from sklearn.linear_model import LinearRegression
import tinydb
import sympy
import numpy as np
import json
from collections import OrderedDict
import itertools
import operator
import copy
from functools import reduce


# Mapping of energy polynomial coefficients to corresponding property coefficients
feature_transforms = {"CPM_FORM": lambda x: -v.T*sympy.diff(x, v.T, 2),
                      "CPM": lambda x: -v.T*sympy.diff(x, v.T, 2),
                      "SM_FORM": lambda x: -sympy.diff(x, v.T),
                      "SM": lambda x: -sympy.diff(x, v.T),
                      "HM_FORM": lambda x: x - v.T*sympy.diff(x, v.T),
                      "HM": lambda x: x - v.T*sympy.diff(x, v.T)}

plot_mapping = {
    'T': 'Temperature (K)',
    'CPM': 'Heat Capacity (J/K-mol-atom)',
    'HM': 'Enthalpy (J/mol-atom)',
    'SM': 'Entropy (J/K-mol-atom)',
    'CPM_FORM': 'Heat Capacity of Formation (J/K-mol-atom)',
    'HM_FORM': 'Enthalpy of Formation (J/mol-atom)',
    'SM_FORM': 'Entropy of Formation (J/K-mol-atom)'
}


def load_datasets(dataset_filenames):
    ds_database = tinydb.TinyDB(storage=tinydb.storages.MemoryStorage)
    for fname in dataset_filenames:
        with open(fname) as file_:
            try:
                ds_database.insert(json.load(file_))
            except ValueError as e:
                print('JSON Error in {}: {}'.format(fname, e))
    return ds_database


def canonical_sort_key(x):
    """
    Wrap strings in tuples so they'll sort.

    Parameters
    ==========
    x : sequence
    """
    return [tuple(i) if isinstance(i, (tuple, list)) else (i,) for i in x]


def canonicalize(configuration, equivalent_sublattices):
    """
    Sort a sequence with symmetry. This routine gives the sequence
    a deterministic ordering while respecting symmetry.

    Parameters
    ==========
    configuration : list
        Sublattice configuration to sort.
    equivalent_sublattices : set of set of int
        Indices of 'configuration' which should be equivalent by symmetry, i.e.,
        [[0, 4], [1, 2, 3]] means permuting elements 0 and 4, or 1, 2 and 3, respectively,
        has no effect on the equivalence of the sequence.

    Returns
    =======
    canonicalized : list
    """
    canonicalized = list(configuration)
    if equivalent_sublattices is not None:
        for subl in equivalent_sublattices:
            subgroup = sorted([configuration[idx] for idx in sorted(subl)], key=canonical_sort_key)
            for subl_idx, conf_idx in enumerate(sorted(subl)):
                if isinstance(subgroup[subl_idx], list):
                    canonicalized[conf_idx] = tuple(subgroup[subl_idx])
                else:
                    canonicalized[conf_idx] = subgroup[subl_idx]

    return tuple(canonicalized)


def _symmetry_filter(x, config, symmetry):
    if x['mode'] == 'manual':
        if len(config) != len(x['sublattice_configurations'][0]):
            return False
        # If even one matches, it's a match
        # We do more filtering downstream
        for data_config in x['sublattice_configurations']:
            if canonicalize(config, symmetry) == canonicalize(data_config, symmetry):
                return True
    return False


def _get_data(comps, phase_name, configuration, symmetry, datasets, prop):
    configuration = list(configuration)
    desired_data = datasets.search((tinydb.where('output') == prop) &
                                   (tinydb.where('components') == comps) &
                                   (tinydb.where('solver').test(_symmetry_filter, configuration, symmetry)) &
                                   (tinydb.where('phases') == [phase_name]))
    # This seems to be necessary because the 'values' member does not modify 'datasets'
    # But everything else does!
    desired_data = copy.deepcopy(desired_data)
    #if len(desired_data) == 0:
    #    raise ValueError('No datasets for the system of interest containing {} were in \'datasets\''.format(prop))

    def recursive_zip(a, b):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return list(recursive_zip(x, y) for x, y in zip(a, b))
        else:
            return list(zip(a, b))

    for idx, data in enumerate(desired_data):
        # Filter output values to only contain data for matching sublattice configurations
        matching_configs = np.array([(canonicalize(sblconf, symmetry) == canonicalize(configuration, symmetry))
                                     for sblconf in data['solver']['sublattice_configurations']])
        matching_configs = np.arange(len(data['solver']['sublattice_configurations']))[matching_configs]
        # Rewrite output values with filtered data
        desired_data[idx]['values'] = np.array(data['values'], dtype=np.float)[..., matching_configs]
        desired_data[idx]['solver']['sublattice_configurations'] = _list_to_tuple(np.array(data['solver']['sublattice_configurations'],
                                                                            dtype=np.object)[matching_configs].tolist())
        try:
            desired_data[idx]['solver']['sublattice_occupancies'] = np.array(data['solver']['sublattice_occupancies'],
                                                                             dtype=np.object)[matching_configs].tolist()
        except KeyError:
            pass
        # Filter out temperatures below 298.15 K (for now, until better refstates exist)
        temp_filter = np.atleast_1d(data['conditions']['T']) >= 298.15
        desired_data[idx]['conditions']['T'] = np.atleast_1d(data['conditions']['T'])[temp_filter]
        # Don't use data['values'] because we rewrote it above; not sure what 'data' references now
        desired_data[idx]['values'] = desired_data[idx]['values'][..., temp_filter, :]
    return desired_data


def _fit_parameters(feature_matrix, data_quantities, feature_tuple):
    """
    Solve Ax = b, where 'feature_matrix' is A and 'data_quantities' is b.

    Parameters
    ==========
    feature_matrix : ndarray (M*N)
        Regressor matrix
    data_quantities : ndarray (M,)
        Response vector
    feature_tuple : tuple
        Polynomial coefficient corresponding to each column of 'feature_matrix'

    Returns
    =======
    parameters : OrderedDict
       Maps 'feature_tuple' to fitted parameter value.
       If a coefficient is not used, it maps to zero.
    """
    # Now generate candidate models; add parameters one at a time
    model_scores = []
    results = np.zeros((len(feature_tuple), len(feature_tuple)))
    clf = LinearRegression(fit_intercept=False, normalize=True)
    for num_params in range(1, feature_matrix.shape[-1] + 1):
        current_matrix = feature_matrix[:, :num_params]
        clf.fit(current_matrix, data_quantities)
        # This may not exactly be the correct form for the likelihood
        # We're missing the "ridge" contribution here which could become relevant for sparse data
        rss = np.square(np.dot(current_matrix, clf.coef_) - data_quantities.astype(np.float)).sum()
        # Compute Aikaike Information Criterion
        # Form valid under assumption all sample variances are equal and unknown
        score = 2*num_params + current_matrix.shape[-2] * np.log(rss)
        model_scores.append(score)
        results[num_params - 1, :num_params] = clf.coef_
        print(feature_tuple[:num_params], 'rss:', rss, 'AIC:', score)
    return OrderedDict(zip(feature_tuple, results[np.argmin(model_scores), :]))


def _get_samples(desired_data):
    all_samples = []
    for data in desired_data:
        temperatures = np.atleast_1d(data['conditions']['T'])
        site_fractions = data['solver'].get('sublattice_occupancies', [[1]])
        site_fraction_product = [reduce(operator.mul, list(itertools.chain(*[np.atleast_1d(f) for f in fracs])), 1)
                                 for fracs in site_fractions]
        # TODO: Subtle sorting bug here, if the interactions aren't already in sorted order...
        # TODO: This also looks like it won't work if we add more than one interaction here
        interaction_product = [f[0] - f[1] for fracs in site_fractions for f in fracs
                               if isinstance(f, list) and len(f) == 2]
        if len(interaction_product) == 0:
            interaction_product = [0]
        comp_features = zip(site_fraction_product, interaction_product)
        all_samples.extend(list(itertools.product(temperatures, comp_features)))
    return all_samples


def _build_feature_matrix(prop, features, desired_data):
    transformed_features = sympy.Matrix([feature_transforms[prop](i) for i in features])
    all_samples = _get_samples(desired_data)
    feature_matrix = np.empty((len(all_samples), len(transformed_features)), dtype=np.float)
    feature_matrix[:, :] = [transformed_features.subs({v.T: temp, 'YS': compf[0],
                                                       'Z': compf[1]}).evalf()
                            for temp, compf in all_samples]
    return feature_matrix


def _endmembers_from_interaction(configuration):
    config = []
    for c in configuration:
        if isinstance(c, (list, tuple)):
            config.append(c)
        else:
            config.append([c])
    return list(itertools.product(*[tuple(c) for c in config]))


def _format_response_data(desired_data, feature_transform, endmembers):
    "Remove lattice stability contribution from data which are properties of formation."
    total_response = []
    for dataset in desired_data:
        values = np.asarray(dataset['values'], dtype=np.object)
        if endmembers is not None:
            value_idx = 0
            for occupancy, config in zip(dataset['solver']['sublattice_occupancies'],
                                         dataset['solver']['sublattice_configurations']):
                multipliers = [reduce(operator.mul, em, 1) for em in _endmembers_from_interaction(occupancy)]
                print('MULTIPLIERS FROM INTERACTION: '+str(multipliers))
                stabilities = [endmembers[em] for em in _endmembers_from_interaction(config)]

                stability = sympy.Add(*[feature_transform(a*b) for a, b in zip(multipliers, stabilities)])
                # Zero out reference state
                refsymbols = set([x for x in stability.free_symbols if x.name.startswith('GHSER')])
                refsymbols = {x: 0 for x in refsymbols}
                stability = stability.subs(refsymbols)
                # for interaction parameters we're trying to fit excess mixing
                values[..., value_idx] -= stability
                value_idx += 1
        total_response.append(values.flatten())
    return total_response


def sigfigs(x, n):
    if x != 0:
        return np.around(x, -(np.floor(np.log10(np.abs(x)))).astype(np.int) + (n - 1))
    else:
        return x


def fit_formation_energy(comps, phase_name, configuration, symmetry,
                         datasets, features=None, endmembers=None):
    """
    Find suitable linear model parameters for the given phase.
    We do this by successively fitting heat capacities, entropies and
    enthalpies of formation, and selecting against criteria to prevent
    overfitting. The "best" set of parameters minimizes the error
    without overfitting.

    Parameters
    ==========
    comps : list of str
        Names of the relevant components.
    phase_name : str
        Name of the desired phase for which the parameters will be found.
    configuration : ndarray
        Configuration of the sublattices for the fitting procedure.
    symmetry : set of set of int or None
        Symmetry of the sublattice configuration.
    datasets : tinydb of Dataset
        All the datasets desired to fit to.
    features : dict (optional)
        Maps "property" to a list of features for the linear model.
        These will be transformed from "GM" coefficients
        e.g., {"CPM_FORM": (v.T*sympy.log(v.T), v.T**2, v.T**-1, v.T**3)}
    endmembers : dict (optional)
        Maps endmember tuple to its enthalpy of formation

    Returns
    =======
    dict of feature: estimated value
    """
    if features is None:
        features = [("CPM_FORM", (v.T * sympy.log(v.T), v.T**2, v.T**-1, v.T**3)),
                    ("SM_FORM", (v.T,)),
                    ("HM_FORM", (sympy.S.One,))
                    ]
        features = OrderedDict(features)
    if any([isinstance(conf, (list, tuple)) for conf in configuration]):
        # Product of all nonzero site fractions in all sublattices
        YS = sympy.Symbol('YS')
        # Product of all binary interaction terms
        Z = sympy.Symbol('Z')
        redlich_kister_features = (YS, YS*Z, YS*(Z**2), YS*(Z**3))
        for feature in features.keys():
            all_features = list(itertools.product(redlich_kister_features, features[feature]))
            features[feature] = [i[0]*i[1] for i in all_features]
        print('ENDMEMBERS FROM INTERACTION: '+str(_endmembers_from_interaction(configuration)))
        if endmembers is None:
            raise ValueError('Endmember dictionary must be specified to compute an interaction parameter')

    parameters = {}
    for feature in features.values():
        for coef in feature:
            parameters[coef] = 0

    # HEAT CAPACITY OF FORMATION
    desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, "CPM_FORM")
    print('CPM_FORM: datasets found: ', len(desired_data))
    if len(desired_data) > 0:
        cp_matrix = _build_feature_matrix("CPM_FORM", features["CPM_FORM"], desired_data)
        data_quantities = np.concatenate([np.asarray(i['values']).flatten() for i in desired_data], axis=-1)
        parameters.update(_fit_parameters(cp_matrix, data_quantities, features["CPM_FORM"]))
    # ENTROPY OF FORMATION
    desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, "SM_FORM")
    print('SM_FORM: datasets found: ', len(desired_data))
    if len(desired_data) > 0:
        sm_matrix = _build_feature_matrix("SM_FORM", features["SM_FORM"], desired_data)
        data_quantities = np.concatenate(_format_response_data(desired_data,
                                                               feature_transforms["SM_FORM"], endmembers), axis=-1)
        # Subtract out the fixed contribution (from CPM_FORM) from our SM_FORM response vector
        all_samples = _get_samples(desired_data)
        fixed_portion = [feature_transforms["SM_FORM"](i).subs({v.T: temp, 'YS': compf[0],
                                                                'Z': compf[1]}).evalf()
                         for temp, compf in all_samples for i in features["CPM_FORM"]]
        fixed_portion = np.array(fixed_portion, dtype=np.float).reshape(len(all_samples), len(features["CPM_FORM"]))
        fixed_portion = np.dot(fixed_portion, [parameters[feature] for feature in features["CPM_FORM"]])
        if endmembers is not None:
            # Evaluate the response minus the lattice stability
            data_quantities = [i.subs({v.T: ixx[0]}).evalf() for i, ixx in zip(data_quantities, all_samples)]
        data_quantities = np.asarray(data_quantities, dtype=np.float)
        parameters.update(_fit_parameters(sm_matrix, data_quantities - fixed_portion, features["SM_FORM"]))
    # ENTHALPY OF FORMATION
    desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, "HM_FORM")
    print('HM_FORM: datasets found: ', len(desired_data))
    if len(desired_data) > 0:
        hm_matrix = _build_feature_matrix("HM_FORM", features["HM_FORM"], desired_data)
        data_quantities = np.concatenate(_format_response_data(desired_data,
                                                               feature_transforms["HM_FORM"], endmembers), axis=-1)
        # Subtract out the fixed contribution (from CPM_FORM+SM_FORM) from our HM_FORM response vector
        all_samples = _get_samples(desired_data)
        fixed_portion = [feature_transforms["HM_FORM"](i).subs({v.T: temp, 'YS': compf[0],
                                                                'Z': compf[1]}).evalf()
                         for temp, compf in all_samples for i in features["CPM_FORM"]+features["SM_FORM"]]
        fixed_portion = np.array(fixed_portion, dtype=np.float).reshape(len(all_samples),
                                                                        len(features["CPM_FORM"]+features["SM_FORM"]))
        fixed_portion = np.dot(fixed_portion, [parameters[feature] for feature in features["CPM_FORM"]+features["SM_FORM"]])
        if endmembers is not None:
            # Evaluate the response minus the lattice stability
            data_quantities = [i.subs({v.T: ixx[0]}).evalf() for i, ixx in zip(data_quantities, all_samples)]
        data_quantities = np.asarray(data_quantities, dtype=np.float)
        parameters.update(_fit_parameters(hm_matrix, data_quantities - fixed_portion, features["HM_FORM"]))
    return parameters


def _translate_endmember_to_array(endmember, variables):
    site_fractions = sorted(variables, key=str)
    frac_array = np.zeros(len(site_fractions))
    for idx, component in enumerate(endmember):
        frac_array[site_fractions.index(v.SiteFraction(site_fractions[0].phase_name, idx, component))] = 1
    return frac_array


def _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod, configuration, x, y):
    import matplotlib.pyplot as plt
    all_samples = np.array(_get_samples(desired_data), dtype=np.object)
    endpoints = _endmembers_from_interaction(configuration)
    fig = plt.figure(figsize=(9, 9))
    if '_FORM' in y:
        # We were passed a Model object with zeroed out reference states
        yattr = y[:-5]
    else:
        yattr = y
    if len(endpoints) == 1:
        # This is an endmember so we can just compute T-dependent stuff
        temperatures = np.array([i[0] for i in all_samples], dtype=np.float)
        temperatures = np.linspace(temperatures.min(), temperatures.max(), num=100)
        endmember = _translate_endmember_to_array(endpoints[0], mod.ast.atoms(v.SiteFraction))[None, None]
        predicted_quantities = calculate(dbf, comps, [phase_name], output=yattr,
                                         T=temperatures, P=101325, points=endmember, model=mod, mode='numpy')
        if y == 'HM' and x == 'T':
            # Shift enthalpy data so that value at minimum T is zero
            predicted_quantities[yattr] -= predicted_quantities[yattr].sel(T=temperatures[0]).values.flatten()
        fig.gca().plot(temperatures, predicted_quantities[yattr].values.flatten(),
                       label='This work', color='k')
        fig.gca().set_xlabel(plot_mapping.get(x, x))
        fig.gca().set_ylabel(plot_mapping.get(y, y))
    elif len(endpoints) == 2:
        # Binary interaction parameter
        first_endpoint = _translate_endmember_to_array(endpoints[0], mod.ast.atoms(v.SiteFraction))
        second_endpoint = _translate_endmember_to_array(endpoints[1], mod.ast.atoms(v.SiteFraction))
        point_matrix = np.linspace(0, 1, num=100)[None].T * second_endpoint + \
            (1 - np.linspace(0, 1, num=100))[None].T * first_endpoint
        # TODO: Real temperature support
        point_matrix = point_matrix[None, None]
        predicted_quantities = calculate(dbf, comps, [phase_name], output=yattr,
                                         T=300, P=101325, points=point_matrix, model=mod, mode='numpy')
        fig.gca().plot(np.linspace(0, 1, num=100), predicted_quantities[yattr].values.flatten(),
                       label='This work', color='k')
        fig.gca().set_xlim((0, 1))
        fig.gca().set_xlabel(str(':'.join(endpoints[0])) + ' to ' + str(':'.join(endpoints[1])))
        fig.gca().set_ylabel(plot_mapping.get(y, y))
    else:
        raise NotImplementedError('No support for plotting configuration {}', configuration)

    for data in desired_data:
        indep_var_data = None
        if x == 'T' or x == 'P':
            indep_var_data = np.array(data['conditions'][x], dtype=np.float).flatten()
        elif x == 'Z':
            interactions = np.array([i[1][1] for i in _get_samples([data])], dtype=np.float)
            indep_var_data = 1 - (interactions+1)/2
        response_data = np.array(data['values'], dtype=np.float).flatten()
        extra_kwargs = {}
        if len(response_data) < 10:
            extra_kwargs['markersize'] = 20
            extra_kwargs['marker'] = '.'
            extra_kwargs['linestyle'] = 'none'

        fig.gca().plot(indep_var_data, response_data, label=data.get('reference', None),
                       **extra_kwargs)
    fig.gca().legend(loc='best')
    fig.canvas.draw()


def plot_parameters(dbf, comps, phase_name, configuration, symmetry, datasets=None):
    em_plots = [('T', 'CPM'), ('T', 'CPM_FORM'), ('T', 'SM'), ('T', 'SM_FORM'),
                ('T', 'HM'), ('T', 'HM_FORM')]
    mix_plots = [('Z', 'HM_FORM'), ('Z', 'SM_FORM')]
    comps = sorted(comps)
    mod = Model(dbf, comps, phase_name)
    # This is for computing properties of formation
    mod_norefstate = Model(dbf, comps, phase_name, parameters={'GHSER'+c.upper(): 0 for c in comps})
    # Zero out ideal mixing contribution for formation properties
    mod_norefstate.models['idmix'] = 0
    # Is this an interaction parameter or endmember?
    if any([isinstance(conf, list) or isinstance(conf, tuple) for conf in configuration]):
        plots = mix_plots
    else:
        plots = em_plots
    for x_val, y_val in plots:
        if datasets is not None:
            desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, y_val)
        else:
            desired_data = []
        if '_FORM' in y_val:
            _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod_norefstate, configuration, x_val, y_val)
        else:
            _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod, configuration, x_val, y_val)


def _list_to_tuple(x):
    def _tuplify(y):
        if isinstance(y, list) or isinstance(y, tuple):
            return tuple(_tuplify(i) if isinstance(i, (list, tuple)) else i for i in y)
        else:
            return y
    return tuple(map(_tuplify, x))


def _tuple_to_list(x):
    def _listify(y):
        if isinstance(y, list) or isinstance(y, tuple):
            return list(y)
        else:
            return y
    return list(map(_listify, x))


def _generate_symmetric_group(configuration, symmetry):
    configurations = [_list_to_tuple(configuration)]
    permutation = np.array(symmetry, dtype=np.object)

    def permute(x):
        if len(x) == 0:
            return x
        x[0] = np.roll(x[0], 1)
        x[:] = np.roll(x, 1, axis=0)
        return x

    while np.any(np.array(symmetry, dtype=np.object) != permute(permutation)):
        new_conf = np.array(configurations[0], dtype=np.object)
        subgroups = []
        # There is probably a more efficient way to do this
        for subl in permutation:
            subgroups.append([configuration[idx] for idx in subl])
        # subgroup is ordered according to current permutation
        # but we'll index it based on the original symmetry
        # This should permute the configurations
        for subl, subgroup in zip(symmetry, subgroups):
            for subl_idx, conf_idx in enumerate(subl):
                new_conf[conf_idx] = subgroup[subl_idx]
        configurations.append(tuple(new_conf))

    return sorted(set(configurations), key=canonical_sort_key)


def phase_fit(dbf, phase_name, symmetry, subl_model, site_ratios, datasets):
    """
    Generate an initial CALPHAD model for a given phase and
    sublattice model.

    Parameters
    ==========
    dbf : Database
        Database to add parameters to.
    phase_name : str
        Name of the phase.
    symmetry : set of set of int or None
        Sublattice model symmetry.
    subl_model : list of tuple
        Sublattice model for the phase of interest.
    site_ratios : list of float
        Number of sites in each sublattice, normalized to one atom.
    datasets : tinydb of datasets
        All datasets to consider for the calculation.
    """
    # First fit endmembers
    all_em_count = len(list(itertools.product(*subl_model)))
    endmembers = sorted(set(canonicalize(i, symmetry) for i in itertools.product(*subl_model)))
    # Number of significant figures in parameters
    numdigits = 6
    em_dict = {}
    print('{0} endmembers ({1} distinct by symmetry)'.format(all_em_count, len(endmembers)))

    def _to_tuple(x):
        if isinstance(x, list) or isinstance(x, tuple):
            return tuple(x)
        else:
            return tuple([x])

    for endmember in endmembers:
        print('ENDMEMBER: '+str(endmember))
        parameters = fit_formation_energy(sorted(dbf.elements), phase_name, endmember, symmetry, datasets)
        refdata = 0
        for subl, ratio in zip(endmember, site_ratios):
            if subl == 'VA':
                continue
            refdata = refdata + ratio * sympy.Symbol('GHSER'+subl)
        fit_eq = sympy.Add(*[sigfigs(value, numdigits) * key for key, value in parameters.items()])
        fit_eq += refdata
        symmetric_endmembers = _generate_symmetric_group(endmember, symmetry)
        print('SYMMETRIC_ENDMEMBERS: ', symmetric_endmembers)
        for em in symmetric_endmembers:
            em_dict[em] = fit_eq
            dbf.add_parameter('G', phase_name, tuple(map(_to_tuple, em)), 0, fit_eq)
    # Now fit all binary interactions
    bin_interactions = list(itertools.combinations(endmembers, 2))
    transformed_bin_interactions = []
    for first_endmember, second_endmember in bin_interactions:
        interaction = []
        for first_occupant, second_occupant in zip(first_endmember, second_endmember):
            if first_occupant == second_occupant:
                interaction.append(first_occupant)
            else:
                interaction.append(tuple(sorted([first_occupant, second_occupant])))
        transformed_bin_interactions.append(interaction)
    bin_interactions = sorted(set(canonicalize(i, symmetry) for i in transformed_bin_interactions),
                              key=canonical_sort_key)
    print('{0} distinct binary interactions'.format(len(bin_interactions)))
    for interaction in bin_interactions:
        ixx = []
        for i in interaction:
            if isinstance(i, (tuple, list)):
                ixx.append(tuple(i))
            else:
                ixx.append(i)
        ixx = tuple(ixx)
        print('INTERACTION: '+str(ixx))
        parameters = fit_formation_energy(sorted(dbf.elements), phase_name, ixx, symmetry, datasets, endmembers=em_dict)
        # Organize parameters by polynomial degree
        degree_polys = np.zeros(10, dtype=np.object)
        for degree in reversed(range(10)):
            check_symbol = sympy.Symbol('YS') * sympy.Symbol('Z')**degree
            keys_to_remove = []
            for key, value in parameters.items():
                if key.has(check_symbol):
                    coef = sigfigs(parameters[key], numdigits) * (key / check_symbol)
                    # Try converting to float to work around a print precision problem with sympy
                    try:
                        coef = float(coef)
                    except TypeError:
                        pass
                    degree_polys[degree] += coef
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                parameters.pop(key)
        print(degree_polys)
        # Insert into database
        symmetric_interactions = _generate_symmetric_group(interaction, symmetry)
        for degree in np.arange(degree_polys.shape[0]):
            if degree_polys[degree] != 0:
                for syminter in symmetric_interactions:
                    dbf.add_parameter('L', phase_name, tuple(map(_to_tuple, syminter)), degree, degree_polys[degree])
    # Now fit ternary interactions


def fit(input_fname, datasets):
    # TODO: Validate input JSON
    data = json.load(open(input_fname))
    dbf = Database()
    dbf.elements = sorted(data['components'])
    # Write reference state to Database
    refstate = getattr(pycalphad.refstates, data['refstate'])
    comp_refs = {c.upper(): refstate[c.upper()] for c in dbf.elements if c.upper() != 'VA'}
    comp_refs['VA'] = 0
    dbf.symbols.update({'GHSER'+c.upper(): data for c, data in comp_refs.items()})
    for phase_name, phase_obj in data['phases'].items():
        # Perform parameter selection and single-phase fitting based on input
        # TODO: Need to pass particular models to include: magnetic, order-disorder, etc.
        symmetry = phase_obj.get('equivalent_sublattices', None)
        # TODO: More advanced phase data searching
        site_ratios = phase_obj['sublattice_site_ratios']
        subl_model = phase_obj['sublattice_model']
        dbf.add_phase(phase_name, {}, site_ratios)
        dbf.add_phase_constituents(phase_name, subl_model)
        # phase_fit() adds parameters to dbf
        phase_fit(dbf, phase_name, symmetry, subl_model, site_ratios, datasets)
    # TODO: Fitting with multi-phase data
    # Do I include experimental data for the first time here, or above in single-phase fit?
    # Do I only fit new degrees of freedom here, or do I refine the existing ones? If the latter, which ones?
    return dbf