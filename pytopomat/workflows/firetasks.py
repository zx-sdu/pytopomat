import shutil
import json
import os
import gzip
import re

from monty.json import MontyEncoder, jsanitize

from pytopomat.analyzer import Vasp2TraceCaller, Vasp2TraceOutput

from fireworks import explicit_serialize, FiretaskBase, FWAction
from fireworks.utilities.fw_serializers import DATETIME_HANDLER

from atomate.utils.utils import env_chk, get_logger
from atomate.vasp.database import VaspCalcDb
from atomate.common.firetasks.glue_tasks import get_calc_loc, PassResult, \
    CopyFiles, CopyFilesFromCalcLoc

logger = get_logger(__name__)


@explicit_serialize
class Vasp2TraceToDb(FiretaskBase):
    """
    Stores data from traces.txt that is output by vasp2trace.

    optional_params:
        db_file (str): path to the db file
    """

    required_params = ["vasp2trace_out"]
    optional_params = ["db_file"]

    def run_task(self, fw_spec):

        d = self["vasp2trace_out"] or fw_spec["vasp2trace_out"]

        d = jsanitize(d)

        # store the results
        db_file = env_chk(self.get("db_file"), fw_spec)
        if not db_file:
            with open("vasp2trace.json", "w") as f:
                f.write(json.dumps(d, default=DATETIME_HANDLER))
        else:
            db = VaspCalcDb.from_db_file(db_file, admin=True)
            db.collection = db.db["vasp2trace"]
            db.collection.insert_one(d)
            logger.info("Vasp2trace calculation complete.")
        return FWAction()


@explicit_serialize
class RunVasp2Trace(FiretaskBase):
    """
    Execute vasp2trace in current directory.

    """

    def run_task(self, fw_spec):

        Vasp2TraceCaller(os.getcwd())

        data = Vasp2TraceOutput(os.getcwd()+'/trace.txt')

        return FWAction(update_spec={"vasp2trace_out": data.as_dict()})


@explicit_serialize
class CopyVaspOutputs(CopyFiles):
    """
    *** This is the same copying class in atomate but altered to accommodate 
    WAVECAR binary files ***

    Copy files from a previous VASP run directory to the current directory.
    By default, copies 'INCAR', 'POSCAR' (default: via 'CONTCAR'), 'KPOINTS', 
    'POTCAR', 'OUTCAR', and 'vasprun.xml'. Additional files, e.g. 'CHGCAR', 
    can also be specified. Automatically handles files that have a ".gz" 
    extension (copies and unzips).

    Note that you must specify either "calc_loc" or "calc_dir" to indicate
    the directory containing the previous VASP run.

    Required params:
        (none) - but you must specify either "calc_loc" OR "calc_dir"

    Optional params:
        calc_loc (str OR bool): if True will set most recent calc_loc. If str
            search for the most recent calc_loc with the matching name
        calc_dir (str): path to dir that contains VASP output files.
        filesystem (str): remote filesystem. e.g. username@host
        additional_files ([str]): additional files to copy,
            e.g. ["CHGCAR", "WAVECAR"]. Use $ALL if you just want to copy
            everything
        contcar_to_poscar(bool): If True (default), will move CONTCAR to
            POSCAR (original POSCAR is not copied).
    """

    optional_params = ["calc_loc", "calc_dir", "filesystem", "additional_files",
                       "contcar_to_poscar"]

    def run_task(self, fw_spec):

        calc_loc = get_calc_loc(self["calc_loc"],
                                fw_spec["calc_locs"]) if self.get(
            "calc_loc") else {}

        # determine what files need to be copied
        files_to_copy = None
        if not "$ALL" in self.get("additional_files", []):
            files_to_copy = ['INCAR', 'POSCAR', 'KPOINTS', 'POTCAR', 'OUTCAR',
                             'vasprun.xml']
            if self.get("additional_files"):
                files_to_copy.extend(self["additional_files"])

        # decide between poscar and contcar
        contcar_to_poscar = self.get("contcar_to_poscar", True)
        if contcar_to_poscar and "CONTCAR" not in files_to_copy:
            files_to_copy.append("CONTCAR")
            files_to_copy = [f for f in files_to_copy if
                             f != 'POSCAR']  # remove POSCAR

        # setup the copy
        self.setup_copy(self.get("calc_dir", None),
                        filesystem=self.get("filesystem", None),
                        files_to_copy=files_to_copy, from_path_dict=calc_loc)
        # do the copying
        self.copy_files()

    def copy_files(self):
        all_files = self.fileclient.listdir(self.from_dir)
        # start file copy
        for f in self.files_to_copy:
            prev_path_full = os.path.join(self.from_dir, f)
            dest_fname = 'POSCAR' if f == 'CONTCAR' and self.get(
                "contcar_to_poscar", True) else f
            dest_path = os.path.join(self.to_dir, dest_fname)

            relax_ext = ""
            relax_paths = sorted(
                self.fileclient.glob(prev_path_full + ".relax*"))
            if relax_paths:
                if len(relax_paths) > 9:
                    raise ValueError(
                        "CopyVaspOutputs doesn't properly handle >9 relaxations!")
                m = re.search('\.relax\d*', relax_paths[-1])
                relax_ext = m.group(0)

            # detect .gz extension if needed - note that monty zpath() did not seem useful here
            gz_ext = ""
            if not (f + relax_ext) in all_files:
                for possible_ext in [".gz", ".GZ"]:
                    if (f + relax_ext + possible_ext) in all_files:
                        gz_ext = possible_ext

            if not (f + relax_ext + gz_ext) in all_files:
                raise ValueError("Cannot find file: {}".format(f))

            # copy the file (minus the relaxation extension)
            self.fileclient.copy(prev_path_full + relax_ext + gz_ext,
                                 dest_path + gz_ext)

            # unzip the .gz if needed
            if gz_ext in ['.gz', ".GZ"]:
                # unzip dest file
                if 'WAVECAR' in dest_path:
                    f = gzip.open(dest_path + gz_ext, 'rb')
                    with open(dest_path, 'wb') as f_out:
                        shutil.copyfileobj(f, f_out)
                    f.close()
                else:
                    f = gzip.open(dest_path + gz_ext, 'rt')
                    file_content = f.read()
                    with open(dest_path, 'w') as f_out:
                        f_out.writelines(file_content)
                    f.close()
                os.remove(dest_path + gz_ext)