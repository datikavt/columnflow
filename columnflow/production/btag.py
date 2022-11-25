# coding: utf-8

"""
Producers for btag scale factor weights.
"""

from __future__ import annotations

from columnflow.production import Producer, producer
from columnflow.util import maybe_import
from columnflow.columnar_util import set_ak_column, flat_np_view, layout_ak_array

np = maybe_import("numpy")
ak = maybe_import("awkward")


@producer(
    uses={
        "Jet.hadronFlavour", "Jet.eta", "Jet.pt", "Jet.btagDeepFlavB",
    },
    # produced columns are defined in the init function below
)
def btag_weights(
    self: Producer,
    events: ak.Array,
    jet_mask: ak.Array | type(Ellipsis) = Ellipsis,
    **kwargs,
) -> ak.Array:
    """
    B-tag scale factor weight producer. Requires an external file in the config as (e.g.)

    .. code-block:: python

        "btag_sf_corr": ("/afs/cern.ch/user/m/mrieger/public/mirrors/jsonpog-integration-d0a522ea/POG/BTV/2017_UL/btagging.json.gz", "v1"),  # noqa

    as well as an auxiliary entry in the config to refer to the b-tag correction set.

    .. code-block:: python

        cfg.x.btag_sf_correction_set = "deepJet_shape"

    In addition, JEC uncertainty sources are propagated and weight columns are written if an
    auxiliary config entry ``btag_sf_jec_sources`` exists.

    Resources:

       - https://twiki.cern.ch/twiki/bin/view/CMS/BTagShapeCalibration?rev=26
       - https://indico.cern.ch/event/1096988/contributions/4615134/attachments/2346047/4000529/Nov21_btaggingSFjsons.pdf
    """
    if self.dataset_inst.is_data:
        return events

    # get flat inputs, evaluated at jet_mask
    flavor = flat_np_view(events.Jet.hadronFlavour[jet_mask], axis=1)
    abs_eta = flat_np_view(abs(events.Jet.eta[jet_mask]), axis=1)
    pt = flat_np_view(events.Jet.pt[jet_mask], axis=1)
    b_discr = flat_np_view(events.Jet.btagDeepFlavB[jet_mask], axis=1)

    # helper to create and store the weight
    def add_weight(syst_name, syst_direction, column_name):
        # define a mask that selects the correct flavor to assign to, depending on the systematic
        flavor_mask = Ellipsis
        if syst_name in ["hf", "lf", "hfstats1", "hfstats2", "lfstats1", "lfstats2"]:
            flavor_mask = flavor != 4
        elif syst_name in ["cferr1", "cferr2"]:
            flavor_mask = flavor == 4

        # get the flat scale factors
        sf_flat = self.btag_sf_corrector.evaluate(
            syst_name if syst_name == "central" else f"{syst_direction}_{syst_name}",
            flavor[flavor_mask],
            abs_eta[flavor_mask],
            pt[flavor_mask],
            b_discr[flavor_mask],
        )

        # insert them into an array of ones whose length corresponds to the total number of jets
        sf_flat_all = np.ones_like(flavor, dtype=np.float32)
        if jet_mask is Ellipsis:
            indices = flavor_mask
        else:
            indices = flat_np_view(jet_mask)
            if flavor_mask is not Ellipsis:
                indices = np.where(indices)[0][flavor_mask]
        sf_flat_all[indices] = sf_flat

        # enforce the correct shape and create the product via all jets per event
        sf = layout_ak_array(sf_flat_all, events.Jet.pt)
        weight = ak.prod(sf, axis=1, mask_identity=False)

        # save the new column
        return set_ak_column(events, column_name, ak.values_astype(weight, np.float32))

    # when the uncertainty is a known jec shift, obtain the propagated effect and do not produce
    # additional systematics
    if self.shift_inst.is_nominal:
        # nominal weight and those of all method intrinsic uncertainties
        events = add_weight("central", None, "btag_weight")
        for syst_name, col_name in self.btag_uncs.items():
            for direction in ["up", "down"]:
                name = col_name.format(year=self.config_inst.campaign.x.year)
                events = add_weight(
                    syst_name,
                    direction,
                    f"btag_weight_{name}_{direction}",
                )
    elif self.shift_is_known_jec_source:
        # TODO: year dependent jec variations fully covered?
        events = add_weight(
            f"jes{'' if self.jec_source == 'Total' else self.jec_source}",
            self.shift_inst.direction,
            f"btag_weight_jec_{self.jec_source}_{self.shift_inst.direction}",
        )
    else:
        # any other shift, just produce the nominal weight
        events = add_weight("central", None, "btag_weight")

    return events


@btag_weights.init
def btag_weights_init(self: Producer) -> None:
    # depending on the requested shift_inst, there are three cases to handle:
    #   1. when a JEC uncertainty is requested whose propagation to btag weights is known, the
    #      producer should only produce that specific weight column
    #   2. when the nominal shift is requested, the central weight and all variations related to the
    #      method-intrinsic shifts are produced
    #   3. when any other shift is requested, only create the central weight column
    if getattr(self, "dataset_inst", None) is None or self.dataset_inst.is_data:
        self.jec_source = None
        self.shift_is_known_jec_source = None
        self.btag_uncs = None
        return

    # to handle this efficiently in one spot, store jec information
    self.jec_source = self.shift_inst.x.jec_source if self.shift_inst.has_tag("jec") else None
    btag_sf_jec_source = "" if self.jec_source == "Total" else self.jec_source
    self.shift_is_known_jec_source = (
        self.jec_source and
        btag_sf_jec_source in self.config_inst.x("btag_sf_jec_sources", [])
    )

    # save names of method-intrinsic uncertainties
    self.btag_uncs = {
        "hf": "hf",
        "lf": "lf",
        "hfstats1": "hfstats1_{year}",
        "hfstats2": "hfstats2_{year}",
        "lfstats1": "lfstats1_{year}",
        "lfstats2": "lfstats2_{year}",
        "cferr1": "cferr1",
        "cferr2": "cferr2",
    }

    # add uncertainty sources of the method itself
    if self.shift_inst.is_nominal:
        # nominal column
        self.produces.add("btag_weight")
        # all varied columns
        for col_name in self.btag_uncs.values():
            name = col_name.format(year=self.config_inst.campaign.x.year)
            for direction in ["up", "down"]:
                self.produces.add(f"btag_weight_{name}_{direction}")
    elif self.shift_is_known_jec_source:
        # jec varied column
        self.produces.add(f"btag_weight_jec_{self.jec_source}_{self.shift_inst.direction}")
    else:
        # only the nominal column
        self.produces.add("btag_weight")


@btag_weights.requires
def btag_weights_requires(self: Producer, reqs: dict) -> None:
    if self.dataset_inst.is_data or "external_files" in reqs:
        return

    from columnflow.tasks.external import BundleExternalFiles
    reqs["external_files"] = BundleExternalFiles.req(self.task)


@btag_weights.setup
def btag_weights_setup(self: Producer, reqs: dict, inputs: dict) -> None:
    if self.dataset_inst.is_data:
        self.btag_sf_corrector = None
        return

    bundle = reqs["external_files"]

    # create the btag sf corrector
    import correctionlib
    correction_set = correctionlib.CorrectionSet.from_string(
        bundle.files.btag_sf_corr.load(formatter="gzip").decode("utf-8"),
    )
    self.btag_sf_corrector = correction_set[self.config_inst.x.btag_sf_correction_set]