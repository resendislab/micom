"""A class representing a microbial or tissue community."""

import re
import six
import cobra
import pandas as pd
from sympy.core.singleton import S
from micom.util import load_model, fluxes_from_primals, add_var_from_expression
from micom.logger import logger
from micom.problems import optcom


_taxonomy_cols = ["id", "file"]


class Community(cobra.Model):
    """A community of models."""

    def __init__(self, taxonomy, id=None, name=None, idmap=None,
                 rel_threshold=1e-6, solver=None):
        """Constructor for the class."""
        super(Community, self).__init__(id, name)

        logger.info("building new mico model {}.".format(id))
        if not solver:
            self.solver = ("cplex" if "cplex" in cobra.util.solver.solvers
                           else "glpk")
        else:
            self.solver = solver

        if not (isinstance(taxonomy, pd.DataFrame) and
                all(col in taxonomy.columns for col in _taxonomy_cols)):
            raise ValueError("`taxonomy` must be a pandas DataFrame with at"
                             "least columns id and file :(")

        self._rtol = rel_threshold
        self._modification = None

        if "abundance" not in taxonomy.columns:
            taxonomy["abundance"] = 1
        taxonomy.abundance /= taxonomy.abundance.sum()
        logger.info("{} models with abundances below threshold".format(
                    (taxonomy.abundance <= self._rtol).sum()))
        taxonomy = taxonomy[taxonomy.abundance > self._rtol]

        self.__taxonomy = taxonomy.copy()
        self.__taxonomy.index = self.__taxonomy.id

        obj = S.Zero
        self.objectives = {}
        for idx, row in self.__taxonomy.iterrows():
            model = load_model(row.file)
            suffix = "__" + idx.replace(" ", "_").strip()
            logger.info("converting IDs for {}".format(idx))
            for r in model.reactions:
                r.global_id = r.id
                r.id += suffix
                r.community_id = idx
            for m in model.metabolites:
                m.global_id = m.id
                m.id += suffix
                m.compartment += suffix
                m.community_id = idx
            logger.info("adding reactions for {} to community".format(idx))
            self.add_reactions(model.reactions)
            o = self.solver.interface.Objective.clone(model.objective,
                                                      model=self.solver)
            obj += o.expression * row.abundance
            self.objectives[idx] = o.expression
            self.__add_exchanges(model.reactions, row)

        com_obj = add_var_from_expression(self, "community_objective",
                                          obj, lb=0)
        self.objective = self.problem.Objective(com_obj, direction="max")

    def __add_exchanges(self, reactions, info):
        """Add exchange reactions for a new model."""
        to_add = []
        for r in reactions:
            if not r.boundary:
                continue
            export = len(r.reactants) == 1
            lb, ub = r.bounds if export else (-r.upper_bound, -r.lower_bound)
            met = (r.reactants + r.products)[0]
            medium_id = re.sub("_{}$".format(met.compartment), "_m", met.id)
            if medium_id == met.id:
                medium_id += "_m"
            if medium_id not in self.metabolites:
                # If metabolite does not exist in medium add it to the model
                # and also add an exchange reaction for the medium
                medium_met = met.copy()
                medium_met.id = medium_id
                medium_met.compartment = "m"
                ex_medium = cobra.Reaction(
                    id="EX_" + medium_met.id,
                    name=medium_met.id + " medium exchange",
                    lower_bound=lb,
                    upper_bound=ub)
                ex_medium.add_metabolites({medium_met: -1})
                ex_medium.global_id = ex_medium.id
                ex_medium.community_id = None
                to_add.append(ex_medium)
            else:
                medium_met = self.metabolites.get_by_id(medium_id)
                ex_medium = self.reactions.get_by_id("EX_" + medium_met.id)
                ex_medium.lower_bound = min(lb, ex_medium.lower_bound)
                ex_medium.upper_bound = max(ub, ex_medium.upper_bound)

            coef = info.abundance
            r.add_metabolites({medium_met: coef if export else -coef})
        self.add_reactions(to_add)

    def __update_exchanges(self):
        """Update exchanges."""
        for met in self.metabolites.query(lambda x: x.compartment == "m"):
            for r in met.reactions:
                if r.boundary:
                    continue
                coef = self.__taxonomy.loc[r.community_id, "abundance"]
                if met in r.products:
                    r.add_metabolites({met: coef}, combine=False)
                else:
                    r.add_metabolites({met: -coef}, combine=False)

    def __update_community_objective(self):
        "Update the community objective."
        v = self.variables.community_objective
        const = self.constraints.community_objective_equality
        self.remove_cons_vars([const])
        com_obj = S.Zero
        for sp, expr in self.objectives.items():
            ab = self.__taxonomy.loc[sp, "abundance"]
            com_obj += ab * expr
        const = self.problem.Constraint(v - com_obj, lb=0, ub=0,
                                        name="community_objective_equality")
        self.add_cons_vars([const])

    def optimize_single(self, id, fluxes=False):
        """Optimize growth rate for a single model in the community."""
        if isinstance(id, six.string_types):
            if id not in self.__taxonomy.index:
                raise ValueError(id + " not in taxonomy!")
            info = self.__taxonomy.loc[id]
        elif isinstance(id, int) and id >= 0 and id < len(self.__taxonomy):
            info = self.__taxonomy.iloc[id]
        else:
            raise ValueError("`id` must be an id or positive index!")

        logger.info("optimizing for {}".format(info.name))

        obj = self.objectives[info.name]
        with self as m:
            m.objective = obj
            m.solver.optimize()
            if fluxes:
                res = fluxes_from_primals(m, info)
            else:
                res = m.objective.value

        return res

    def optimize_all(self, fluxes=False):
        """Return solutions for individually optimizing each model."""
        individual = (self.optimize_single(id, fluxes) for id in
                      self.__taxonomy.index)

        if fluxes:
            return pd.concat(individual, axis=1).T
        else:
            return pd.Series(individual, self.__taxonomy.index)

    @property
    def abundances(self):
        return self.__taxonomy.abundance

    @abundances.setter
    def abundances(self, value):
        try:
            self.__taxonomy.abundance = value
        except Exception:
            raise ValueError("value must be an iterable with an entry for "
                             "each species/tissue")

        ab = self.__taxonomy.abundance
        self.__taxonomy.abundance /= ab.sum()
        small = ab < self._rtol
        self.__taxonomy.loc[small, "abundance"] = self._rtol
        self.__update_exchanges()
        self.__update_community_objective()

    @property
    def taxonomy(self):
        return self.__taxonomy.copy()

    @property
    def modification(self):
        return self._modification

    @modification.setter
    @cobra.util.context.resettable
    def modification(self, mod):
        self._modification = mod

    def optcom(self, strategy="lagrangian", min_growth=0.1, tradeoff=0.5,
               fluxes=False, pfba=True):
        """Run optcom for the community."""
        return optcom(self, strategy, min_growth, tradeoff, fluxes, pfba)
