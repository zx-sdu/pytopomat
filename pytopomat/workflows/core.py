import numpy as np

from uuid import uuid4

from pymatgen.io.vasp.inputs import Kpoints
from pymatgen.io.vasp.sets import MPStaticSet

from atomate.vasp.config import STABILITY_CHECK, VASP_CMD, DB_FILE, ADD_WF_METADATA
from atomate.vasp.powerups import (
    add_stability_check,
    add_modify_incar,
    add_wf_metadata,
    add_common_powerups,
    add_additional_fields_to_taskdocs,
    add_tags,
)
from atomate.vasp.workflows.base.core import get_wf
from atomate.vasp.fireworks.core import OptimizeFW, StaticFW

from fireworks import Workflow

from pytopomat.analyzer import StructureDimensionality
from pytopomat.z2pack_caller import Z2PackCaller, Z2Output
from pytopomat.workflows.fireworks import Z2PackFW

"""
This module provides workflows for running high-throughput calculations.
"""

__author__ = "Jason Munro, Nathan C. Frey"
__copyright__ = "MIT License"
__version__ = "0.0.1"
__maintainer__ = "Jason Munro, Nathan C. Frey"
__email__ = "jmunro@lbl.gov, ncfrey@lbl.gov"
__status__ = "Development"
__date__ = "August 2019"


def wf_vasp2trace_nonmagnetic(structure, c=None):
    """
        Fireworks workflow for running a vasp2trace calculation on a nonmagnetic material.

        Args:
            structure (Structure): Pymatgen structure object

        Returns:
            Workflow
    """

    c = c or {}
    vasp_cmd = c.get("VASP_CMD", VASP_CMD)
    db_file = c.get("DB_FILE", DB_FILE)

    ncoords = 3 * len(structure.sites)

    nbands = 0

    for site in structure.sites:
        nbands += site.species.total_electrons

    trim_kpoints = Kpoints(
        comment="TRIM Points",
        num_kpts=8,
        style=Kpoints.supported_modes.Reciprocal,
        kpts=(
            (0, 0, 0),
            (0.5, 0, 0),
            (0, 0.5, 0),
            (0, 0, 0.5),
            (0.5, 0.5, 0),
            (0, 0.5, 0.5),
            (0.5, 0, 0.5),
            (0.5, 0.5, 0.5),
        ),
        kpts_shift=(0, 0, 0),
        kpts_weights=[1, 1, 1, 1, 1, 1, 1, 1],
        coord_type="Reciprocal",
        labels=["gamma", "x", "y", "z", "s", "t", "u", "r"],
        tet_number=0,
        tet_weight=0,
        tet_connections=None,
    )

    wf = get_wf(
        structure,
        "vasp2trace_nonmagnetic.yaml",
        params=[
            {},
            {},
            {
                "input_set_overrides": {
                    "other_params": {"user_kpoints_settings": trim_kpoints}
                }
            },
            {},
        ],
        vis=MPStaticSet(structure, potcar_functional="PBE_54", force_gamma=True),
        common_params={"vasp_cmd": vasp_cmd, "db_file": db_file},
    )

    dim_data = StructureDimensionality(structure)

    if np.any(
        [
            dim == 2
            for dim in [dim_data.larsen_dim, dim_data.cheon_dim, dim_data.gorai_dim]
        ]
    ):
        wf = add_modify_incar(
            wf,
            modify_incar_params={
                "incar_update": {"IVDW": 11, "EDIFFG": 0.005, "IBRION": 2, "NSW": 100}
            },
            fw_name_constraint="structure optimization",
        )
    else:
        wf = add_modify_incar(
            wf,
            modify_incar_params={
                "incar_update": {"EDIFFG": 0.005, "IBRION": 2, "NSW": 100}
            },
            fw_name_constraint="structure optimization",
        )

    wf = add_modify_incar(
        wf,
        modify_incar_params={
            "incar_update": {"ADDGRID": ".TRUE.", "LASPH": ".TRUE.", "GGA": "PS"}
        },
    )

    wf = add_modify_incar(
        wf,
        modify_incar_params={
            "incar_update": {
                "ISYM": 2,
                "LSORBIT": ".TRUE.",
                "MAGMOM": "%i*0.0" % ncoords,
                "ISPIN": 1,
                "LWAVE": ".TRUE.",
                "NBANDS": nbands,
            }
        },
        fw_name_constraint="nscf",
    )

    wf = add_common_powerups(wf, c)

    if c.get("STABILITY_CHECK", STABILITY_CHECK):
        wf = add_stability_check(wf, fw_name_constraint="structure optimization")

    if c.get("ADD_WF_METADATA", ADD_WF_METADATA):
        wf = add_wf_metadata(wf, structure)

    return wf


class Z2PackWF:
    def __init__(self, structure, vasp_cmd=VASP_CMD, db_file=DB_FILE, name="Z2Pack WF"):
        """
      ***VASP_CMD in my_fworker.yaml MUST be set to "vasp_ncl" for Z2Pack.

      Fireworks workflow for running Z2Pack to compute Z2 invariants and Chern numbers.

      Args:
          structure (Structure): Pymatgen structure object

      """

        self.structure = structure
        self.uuid = str(uuid4())
        self.wf_meta = {"wf_uuid": self.uuid, "wf_name": self.__class__.__name__}

    def get_wf(self, c=None):
        """Get the workflow.

        Returns:
          Workflow

        """

        c = c or {"VASP_CMD": VASP_CMD, "DB_FILE": DB_FILE}
        vasp_cmd = c.get("VASP_CMD", VASP_CMD)
        db_file = c.get("DB_FILE", DB_FILE)

        nsites = len(self.structure.sites)

        opt_fw = OptimizeFW(
            self.structure, vasp_cmd=c["VASP_CMD"], db_file=c["DB_FILE"]
        )

        static_fw = StaticFW(
            self.structure,
            vasp_cmd=c["VASP_CMD"],
            db_file=c["DB_FILE"],
            parents=[opt_fw],
        )

        z2pack_fw = Z2PackFW(
            parents=[opt_fw, static_fw],
            structure=self.structure,
            uuid=self.uuid,
            vasp_cmd=c["VASP_CMD"],
            db_file=c["DB_FILE"],
        )

        fws = [opt_fw, static_fw, z2pack_fw]

        wf = Workflow(fws)
        wf = add_additional_fields_to_taskdocs(wf, {"wf_meta": self.wf_meta})

        # Add vdW corrections if structure is layered
        dim_data = StructureDimensionality(self.structure)

        if np.any(
            [
                dim == 2
                for dim in [dim_data.larsen_dim, dim_data.cheon_dim, dim_data.gorai_dim]
            ]
        ):
            wf = add_modify_incar(
                wf,
                modify_incar_params={
                    "incar_update": {
                        "IVDW": 11,
                        "EDIFFG": 0.005,
                        "IBRION": 2,
                        "NSW": 100,
                    }
                },
                fw_name_constraint="structure optimization",
            )

            wf = add_modify_incar(
                wf,
                modify_incar_params={"incar_update": {"IVDW": 11}},
                fw_name_constraint="static",
            )

            wf = add_modify_incar(
                wf,
                modify_incar_params={"incar_update": {"IVDW": 11}},
                fw_name_constraint="z2pack",
            )

        else:
            wf = add_modify_incar(
                wf,
                modify_incar_params={
                    "incar_update": {"EDIFFG": 0.005, "IBRION": 2, "NSW": 100}
                },
                fw_name_constraint="structure optimization",
            )

        # Helpful vasp settings
        wf = add_modify_incar(
            wf,
            modify_incar_params={
                "incar_update": {"ADDGRID": ".TRUE.", "LASPH": ".TRUE.", "GGA": "PS"}
            },
        )

        # Generate inputs for Z2Pack with a static calc
        wf = add_modify_incar(
            wf,
            modify_incar_params={"incar_update": {"PREC": "Accurate"}},
            fw_name_constraint="static",
        )

        wf = add_common_powerups(wf, c)

        if c.get("STABILITY_CHECK", STABILITY_CHECK):
            wf = add_stability_check(wf, fw_name_constraint="structure optimization")

        if c.get("ADD_WF_METADATA", ADD_WF_METADATA):
            wf = add_wf_metadata(wf, self.structure)

        tag = "z2pack: {}".format(self.uuid)
        wf = add_tags(wf, [tag])

        return wf
