# Stuff to add

## Chemical potential
- Update chemical potential tools to work with new Materials Project API. Currently, supplying an API key for the new Materials Project API returns entries which do not have `e_above_hull` as a property, and so crashes. Ideally would be good to be compatible with both the legacy and new API, which should be fairly straightforward (just detect which type of API key is present like pymatgen does, and go from there). See https://docs.materialsproject.org/downloading-data/using-the-api 
- Note about cost of `vasp_ncl` chemical potential calculations for metals, use `ISMEAR = -5`, possibly `NKRED` etc. (make a function to generate `vasp_ncl` calculation files with `ISMEAR = -5`, with option to set different kpoints) - if `ISMEAR = 0` - converged kpoints still prohibitively large, use vasp_converge_files again to check for quicker convergence with ISMEAR = -5.
- Use `NKRED = 2` for `vasp_ncl` chempot calcs, if even kpoints and over 4. Often can't use `NKRED` with `vasp_std`, because we don't know beforehand the kpts in the IBZ (because symmetry on for `vasp_std` chempot calcs)(same goes for `EVENONLY = True`).
- Add `chempot_std_to_ncl.sh` bash script to auto-generate symmetrised `KPOINTS` for SOC `vasp_ncl` run from `vasp_std` IBZKPT, and copy `vasp_std/CONTCAR` to `vasp_ncl/POSCAR`, copy `CHGCAR`, `POTCAR` over etc. (Make note about symmetrised k-points required for accurate SOC bandstructures, VASP wizardry with Chris, but not an issue for ground state energies).
- Use `UserChemPotAnalyzer` to parse chemical potential calculations
- Note about SOC for chemical potential calculations (Lany says: to ‘a good approximation’, the SOC contributions to total energy can be separated into purely atomic contributions, Lany, Stevanovic and Zunger show in their [FERE paper](https://doi.org/10.1103/PhysRevB.85.115104) that the SOC effects on total energy cancel out for chemical potential calculations) - But only for easy systems - better to do consistently
- Publication ready chemical potential diagram plotting tool (see `doped_chempot_plotting_example.ipynb`; code there, just needs to be implemented in module functions).

## Defect calculations set up
- Check supercell generation algorithm, worth using ASE's optimal supercell generation tools?
- Note about `ISPIN = 1` for even no. of electrons defect species, **if you're sure there's no magnetic ordering!**
- `NKRED` pre-relaxing on defect structures (see jspark Slack discussion)
- Option _not to set_ certain `INCAR` tags (like HFSCREEN and LORBIT, cause their default "None" doesn't really correspond to a certain value; could add a `remove_incar_tags` arg and then `pop` them out of the incar dict?)
- create a SMTG_defects_input_set for different functionals (PBE0, HSE0, PBE) and maybe just use `DictSet` base class rather than one of the pre-existing classes to make the vasp input files.
- Streamline vasp_input functions (prepare_vasp_defect_inputs and prepare_vasp_defect_dict should all be done in one, remove hard-coded tags from the functions)
- Print Wyckoff position of proposed interstitial sites (and optional output of Wyckoff sites which are neither atomic nor Voronoi sites)
- Better charge state predictor? At least print determined oxidation state ranges, and warning that you're gonna use these to predict defect charge states (so people can see if something off etc.); could use the csv Dan sent on defects slack (17 Mar 21 - this can also be done in pymatgen; see ShakeNBreak most_common_oxi function) and set an arbitrary cutoff for oxidation states that can occur in known materials. Alternative possibility is do +/-2 to fully-ionised+/-2, as this should cover >99% of amphoteric cases right? (See emails with Jimmy – can be easily done with 'padding' option in pymatgen-analysis-defects?)
- Multiprocessing ability for interstitial generation. Perhaps symmetry reduction methods, where you first reduce the initial structure via symmetry to the primitive cell, then do interstitial generation, then convert to interstitials in initial supercell structure.
- Ideally figure out automation of polaron finding
- Add function to post-process and remove closely-located interstitials for structures with large voids (from SMTG #software Slack (Yong-Seok): "If your structure has large space for interstitials and it predicts lots of atoms closely positioned to each other (& take longer time to predict), you can increase min_dist  (default is 0.5) in remove_collisions function in [python path]/python3.9/site-packages/pymatgen/analysis/defects/utils.py"), and add note to example notebooks about this.
- Functions for generating input files, parsing (with GKFO correction) and plotting the results (i.e. configuration coordinate diagrams) of optical calculations. Integrate with Joe's `config-coord-plots`? (also see `CarrierCapture` functionalities)
- Currently inputting multiple extrinsic `sub_species` will assume you are co-doping, and will output competing phases for this (e.g. K and In with BaSnO3 will output KInO2), default should not be to do this, but have an optional argument for co-doping treatment.

## Post-processing / analysis / plotting

- Change `get_stdrd_metadata` to a semi-hidden method and call in `SingleDefectParser.from_paths()` to avoid extra/redundant function calls by user.
- `aide` labelling of defect species in formation energy plots.
- Note that if you edit the entries in a DefectPhaseDiagram after creating it, you need to `dpd.find_stable_charges()` to update the transition level map etc.
- `transition_levels_table()`
- Change formation energy plotting and tabulation to DefectPhaseDiagram methods rather than standalone functions.
- Fix `(ax=ax)` optional parameter behaviour in `formation_energy_plot` (where `f, ax = plt.subplots` run previously).
- Add warning for bandfilling correction based off energy range of the CBM/VBM occupation? (In addition to num_hole and num_electron)
- Functions for generating input files, parsing (with GKFO correction) and plotting the results (i.e. configuration coordinate diagrams) of optical calculations.
- Functionality to generate chemical potential limit plots from parsed chempot calculations (phase diagram objects), as in Adam Jackson's `plot-cplap-ternary` (3D) and Sungyhun's `cplapy` (4D). – See `Cs2SnTiI6` notebooks for template code for this.
- Figure out a neat way of plotting phase diagrams for quaternary and quinary systems.
- Option for degeneracy-weighted ('reduced') formation energy diagrams, similar to reduced energies in SOD. See Slack discussion and CdTe pyscfermi notebooks.
- Improved methods for estimating/determining the final site degeneracy/multiplicity from relaxed structures. See `pydefect` for tools for this. Also add consideration of odd/even number of electrons to account for spin degeneracy.
- Brouwer diagrams
- Function(s) for exporting defect energies and corrections as Pandas DataFrame / HDF5 / json / yaml / csv etc for readily-accessible, easy-to-use reproducibility
- Functions to output data and python objects to plug and play with `py-sc-fermi`, `AiiDA`, `CarrierCapture`.
- Parsing capability for (non-defect) polarons, so they can then be plotted alongside defects on formation energy diagrams.
- Add warning if, when parsing, only one charge state for a defect is parsed (i.e. the other charge states haven't completed), in case this isn't noticed by the user. Print a list of all parsed charge states as a check.
- Used `monty` JSON encoding rather than `pickle` (like `pymatgen`) as less senstitive to environment and human-readable.
- Automatically check the 'bulk' and 'defect' calculations used the same INCAR tags, and warn user if not.
- Improved handling of the delocalisation analysis warning. `pymatgen`'s version is too sensitive. Maybe if `pymatgen` finds the defect to be incompatible, check how much the error is, and if small enough ignore and force the charge correction function to run (current method is to warn user this is the case, and to check it manually).

## Housekeeping

- Update to be compatible with new `pymatgen` – will need to think about an appropriate naming scheme for inequivalent defect sites as this isn't currently in `pymatgen-analysis-defects` (include site symmetry and multiplicity in name, and if this is still a duplicate then append with "_a", "_b" etc?)(i.e. use this rather than old PyCDT number counting, as this can be easily confused with charge states)
- Modularity - could have defect_creation (what is now vasp input+pycdt), defect_analysis, chempot and a separate plotting module?
- Create GGA practice workflow, for people to learn how to work with doped and defect calculations
- Add `dope_stuff` examples and documentation.
- Add tests
- Clean the example jupyter notebooks and docstrings
- Ready to be used in conjunction with `atomate`, `AiiDA`, `CarrierCapture`.
- PR to pymatgen: Update entry.parameters["kumagai_meta"] = (dict(self.metadata)) to entry.parameters["kumagai_meta"].update(dict(self.metadata)) in KumagaiCorrection.get_correction() in pymatgen/analysis/defects/corrections.py so pymatgen doesn't remove the other relevant kumagai_meta (kumagai_electrostatic etc.) when we run KumagaiCorrection.get_correction(defect_entry) (via finite_size_charge_correction.get_correction_kumagai(defect_entry...)) – see https://github.com/materialsproject/pymatgen-analysis-defects/issues/47
- At present, we can't update to be compatible with the most recent `pymatgen` because the defect corrections code has all been removed and is not yet in `pymatgen-analysis-defects`. Once it is however, we should refactor to be compatible with this. For doing this, worth looking at how this was done for `ShakeNBreak`, and should use the new naming system built in `ShakeNBreak`. When doing so, update to use the `ShakeNBreak` voronoi node-finding functions, as this has been made to be more efficient than the `doped` version (which is already far more efficient than the original...) and isn't available in current `pymatgen`.
