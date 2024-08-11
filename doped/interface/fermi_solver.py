"""
Module for solving the Fermi level and defect concentrations self-consistently
over various parameter spaces including chemical potentials, temperatures, and
effective_dopant_concentrations.

This is done using both Doped and py-sc-fermi. The aim of this module is to
implement a unified interface for both "backends".
"""

import contextlib
import warnings
from copy import deepcopy
from itertools import product
from typing import TYPE_CHECKING, Any, Optional, Union

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from monty.json import MSONable
from pymatgen.electronic_structure.dos import FermiDos, Spin
from pymatgen.io.vasp import Vasprun
from scipy.interpolate import griddata
from scipy.spatial import ConvexHull, Delaunay

from doped.thermodynamics import (
    _add_effective_dopant_concentration,
    _parse_chempots,
    get_rich_poor_limit_dict,
)

if TYPE_CHECKING:
    from pymatgen.util.typing import PathLike

    from doped.thermodynamics import DefectThermodynamics

    with contextlib.suppress(ImportError):
        from py_sc_fermi.defect_species import DefectSpecies
        from py_sc_fermi.defect_system import DefectSystem


def _get_label_and_charge(name: str) -> tuple[str, int]:
    """
    Extracts the label and charge from a defect name string.

    Args:
        name (str): Name of the defect.

    Returns:
        tuple: A tuple containing the label and charge.
    """
    last_underscore = name.rfind("_")
    label = name[:last_underscore] if last_underscore != -1 else name
    charge_str = name[last_underscore + 1 :] if last_underscore != -1 else None

    # Initialize charge with a default value
    charge = 0
    if charge_str is not None:
        with contextlib.suppress(ValueError):
            charge = int(charge_str)
            # If charge_str cannot be converted to int, charge remains its default value

    return label, charge


# TODO: Update DefectThermodynamics docstrings after `py-sc-fermi` interface written, to point toward it
#  for more advanced analysis


def get_py_sc_fermi_dos_from_fermi_dos(
    fermi_dos: FermiDos, vbm: float = None, nelect: int = None, bandgap: float = None
) -> "DOS":
    """
    Given an input ``pymatgen`` ``FermiDos`` object, return a corresponding
    ``py-sc-fermi`` ``DOS`` object (which can then be used with the ``py-sc-
    fermi`` ``FermiSolver`` backend).

    Args:
        TODO
    """
    try:
        from py_sc_fermi.dos import DOS
    except ImportError as exc:
        raise ImportError("py-sc-fermi must be installed to use this function!") from exc

    densities = fermi_dos.densities
    if vbm is None:  # tol 1e-4 is lowest possible, as VASP rounds to 4 dp:
        vbm = fermi_dos.get_cbm_vbm(tol=1e-4, abs_tol=True)[1]

    edos = fermi_dos.energies - vbm
    if len(densities) == 2:
        dos = np.array([densities[Spin.up], densities[Spin.down]])
        spin_pol = True
    else:
        dos = np.array(densities[Spin.up])
        spin_pol = False

    if nelect is None:
        # this requires the input dos to be a FermiDos. NELECT could be calculated alternatively
        # by integrating the tdos of a ``pymatgen`` ``Dos`` object, but this isn't expected to be
        # a common use case and using parsed NELECT from vasprun.xml(.gz) is more reliable
        nelect = fermi_dos.nelecs
    if bandgap is None:
        bandgap = fermi_dos.get_gap(tol=1e-4, abs_tol=True)

    return DOS(dos=dos, edos=edos, nelect=nelect, bandgap=bandgap, spin_polarised=spin_pol)


class FermiSolver(MSONable):
    def __init__(
        self,
        defect_thermodynamics: "DefectThermodynamics",
        backend: Optional[str] = None,
        bulk_dos: Optional[Union[FermiDos, Vasprun, "PathLike"]] = None,
        chempots: Optional[dict] = None,
        el_refs: Optional[dict] = None,
        skip_check: bool = False,
    ):
        """
        Class to calculate the Fermi level, defect and carrier concentrations
        under various conditions, using the input ``DefectThermodynamics``
        object.

        This constructor initializes a ``FermiSolver`` object, setting up the
        necessary attributes, which includes loading the bulk density of states
        (DOS) data from the provided VASP output.

        Args:
            defect_thermodynamics (DefectThermodynamics):
                A ``DefectThermodynamics`` object, providing access to defect
                formation energies and other related thermodynamic properties.
            backend (Optioan[str]):
                The code backend to use for the thermodynamic calculations, which
                can be either ``"doped"`` or ``"py-sc-fermi"``. ``"py-sc-fermi"``
                allows the use of ``free_defects`` for advanced constrained defect
                equilibria (i.e. mobile defects, see advanced thermodynamics tutorial),
                while ``"doped"`` can be quicker. # TODO: Check!
                Default is to use ``py-sc-fermi`` if installed, else ``doped``.
            bulk_dos (FermiDos or Vasprun or PathLike):
                Either a path to the ``vasprun.xml(.gz)`` output of a bulk DOS
                calculation in VASP, a ``pymatgen`` ``Vasprun`` object or a
                ``pymatgen`` ``FermiDos`` for the bulk electronic DOS, for
                calculating carrier concentrations.

                Usually this is a static calculation with the `primitive` cell of
                the bulk material, with relatively dense `k`-point sampling
                (especially for materials with disperse band edges) to ensure an
                accurately-converged DOS and thus Fermi level. ``ISMEAR = -5``
                (tetrahedron smearing) is usually recommended for best convergence
                wrt `k`-point sampling. Consistent functional settings should be
                used for the bulk DOS and defect supercell calculations.
            chempots (Optional[dict]):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations and Fermi level), under
                different chemical environments.

                If ``None`` (default), will use ``DefectThermodynamics.chempots``
                (= 0 for all chemical potentials by default, if not set).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (Optional[dict]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in ``DefectThermodynamics`` in the format generated by
                ``doped`` (see tutorials), or if ``DefectThermodynamics.el_refs`` has been
                set.(Default: None)
            multiplicity_scaling (Optional[float]):
                A scaling factor for defect multiplicity to adjust for differences in
                supercell volumes between the defect calculations and the bulk DOS calculation.
                If None, it is automatically calculated.
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from
                ``DefectThermodynamics.vbm`` by >0.05 eV. Should only be used when the
                reason for this difference is known/acceptable. (default: False)
        """
        self.defect_thermodynamics = defect_thermodynamics
        fermi_dos = self.defect_thermodynamics._parse_fermi_dos(bulk_dos, skip_check=skip_check)
        self.volume = fermi_dos.volume

        if backend is None or "fermi" in backend.lower():
            try:
                from py_sc_fermi.dos import DOS

                self.backend = "py-sc-fermi"
            except ImportError as exc:
                if "fermi" in backend.lower():  # py-sc-fermi explicitly chosen but not installed
                    raise ImportError(
                        "py-sc-fermi is not installed, so only the doped FermiSolver backend is available."
                    ) from exc
                self.backend = "doped"
        elif "fermi" in backend.lower():
            self.backend = "py-sc-fermi"
        elif backend.lower() == "doped":
            self.backend = "doped"
        else:
            raise ValueError(f"Unrecognised `backend`: {backend}")

        if self.backend == "doped":
            self.bulk_dos = fermi_dos
            self.multiplicity_scaling = 1
            self._DefectSystem = self._DefectSpecies = self._DefectChargeState = self._DOS = None
        else:  # py-sc-fermi:
            from py_sc_fermi.defect_charge_state import DefectChargeState
            from py_sc_fermi.defect_species import DefectSpecies
            from py_sc_fermi.defect_system import DefectSystem
            from py_sc_fermi.dos import DOS

            self._DefectSystem = DefectSystem
            self._DefectSpecies = DefectSpecies
            self._DefectChargeState = DefectChargeState
            self._DOS = DOS

            self.bulk_dos = get_py_sc_fermi_dos_from_fermi_dos(
                fermi_dos, vbm=self.defect_thermodynamics.vbm, bandgap=self.defect_thermodynamics.band_gap
            )
            ms = (
                next(iter(self.defect_thermodynamics.defect_entries)).sc_entry.structure.volume
                / self.volume
            )
            if not np.isclose(ms, round(ms), atol=3e-2):  # check multiplicity scaling is almost an integer
                warnings.warn(
                    f"The detected volume ratio ({ms:.3f}) between the defect supercells and the DOS calculation cell is "
                    f"non-integer, indicating that they correspond to (slightly) different unit cell volumes. This can "
                    f"cause quantitative errors of a similar relative magnitude in the predicted defect/carrier concentrations!"
                )
            self.multiplicity_scaling = round(ms)

        # Parse chemical potentials, either using input values (after formatting them in the doped format)
        # or using the class attributes if set:
        self.chempots, self.el_refs = _parse_chempots(
            chempots or self.defect_thermodynamics.chempots, el_refs or self.defect_thermodynamics.el_refs
        )

        if self.chempots is None:
            raise ValueError(
                "You must supply a chemical potentials dictionary or have them present in the "
                "DefectThermodynamics object."
            )

    def _check_required_backend_and_error(self, required_backend: str):
        """
        Check if ``required_backend`` matches ``self.backend``, and throw an
        error message if not.

        Args:
            required_backend (str):
                Backend choice ("doped" or "py-sc-fermi") required.
        """
        if required_backend.lower() != self.backend:
            raise RuntimeError(
                f"This function is only supported for the {required_backend} backend, but you are "
                f"using the {self.backend} backend!"
            )

    def _get_fermi_level_and_carriers(
        self,
        chempots: dict[str, float],
        temperature: float,
        effective_dopant_concentration: Optional[float] = None,
    ) -> tuple[float, float, float]:
        """
        Calculate the Fermi level and carrier concentrations under a given
        chemical potential regime and temperature.

        Args:
            chempots (dict[str, float]):
                A dictionary of chemical potentials for the elements.
                The keys are element symbols, and the values are their
                corresponding chemical potentials.
            temperature (float):
                The temperature at which to solve for the Fermi level
                and carrier concentrations, in Kelvin.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.

        Returns:
            tuple[float, float, float]: A tuple containing:
                - The Fermi level (float) in eV.
                - The electron concentration (float) in cm^-3.
                - The hole concentration (float) in cm^-3.
        """
        self._check_required_backend_and_error("doped")
        fermi_level, electrons, holes = self.defect_thermodynamics.get_equilibrium_fermi_level(  # type: ignore
            bulk_dos=self.bulk_dos,
            chempots=chempots,
            limit=None,
            temperature=temperature,
            return_concs=True,
            effective_dopant_concentration=effective_dopant_concentration,
        )
        return fermi_level, electrons, holes

    def _get_limits(self, limit):
        limit_dict = get_rich_poor_limit_dict(self.chempots)
        limit = next(v for k, v in limit_dict.items() if k == limit)
        return self.chempots["limits_wrt_el_refs"][limit]

    def equilibrium_solve(
        self,
        chempots: dict[str, float],
        temperature: float,
        effective_dopant_concentration: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        Calculate the Fermi level and defect/carrier concentrations under
        `thermodynamic equilibrium`, given a set of chemical potentials and a
        temperature.

        Typically not intended for direct usage, as the same functionality
        is provided by
        ``DefectThermodynamics.get_equilibrium_concentrations/fermi_level()``.

        Args:
            chempots (dict[str, float]):
                A dictionary containing the chemical potentials for the elements.
                The keys are element symbols, and the values are their corresponding
                chemical potentials.
            temperature (float):
                The temperature at which to solve for defect concentrations, in Kelvin.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.

        Returns:
        # TODO: Check this output format matches for both backends!
            pd.DataFrame:
                A DataFrame containing the defect and carrier concentrations, as well
                as the self-consistent Fermi energy and additional properties such as
                electron and hole concentrations. The columns include:
                    - "Fermi Level": The self-consistent Fermi level in eV.
                    - "Electrons (cm^-3)": The electron concentration.
                    - "Holes (cm^-3)": The hole concentration.
                    - "Temperature": The temperature at which the calculation was performed.
                    - "Dopant (cm^-3)": The dopant concentration, if applicable.
                    - "Defect": The defect type.
                    - "Concentration (cm^-3)": The concentration of the defect in cm^-3.
                Additional columns may include concentrations for specific defects and carriers.
        """
        if self.backend == "doped":
            fermi_level, electrons, holes = self._get_fermi_level_and_carriers(
                chempots=chempots,
                temperature=temperature,
                effective_dopant_concentration=effective_dopant_concentration,
            )
            concentrations = self.defect_thermodynamics.get_equilibrium_concentrations(
                chempots=chempots,
                fermi_level=fermi_level,
                temperature=temperature,
                per_charge=False,
                per_site=False,
                skip_formatting=False,
            )
            concentrations = _add_effective_dopant_concentration(
                concentrations, effective_dopant_concentration
            )
            new_columns = {
                "Fermi Level": fermi_level,
                "Electrons (cm^-3)": electrons,
                "Holes (cm^-3)": holes,
                "Temperature": temperature,
            }
            for column, value in new_columns.items():
                concentrations[column] = value
            excluded_columns = ["Defect", "Charge", "Charge State Population"]
            for column in concentrations.columns.difference(excluded_columns):
                concentrations[column] = concentrations[column].astype(float)
            return concentrations

        # else py-sc-fermi backend:
        defect_system = self._generate_defect_system(
            chempots=chempots,
            temperature=temperature,
            effective_dopant_concentration=effective_dopant_concentration,
        )

        with np.errstate(all="ignore"):
            conc_dict = defect_system.concentration_dict()
        data = []

        for k, v in conc_dict.items():
            if k not in ["Fermi Energy", "n0", "p0", "Dopant"]:
                row = {
                    "Temperature": defect_system.temperature,
                    "Fermi Level": conc_dict["Fermi Energy"],
                    "Holes (cm^-3)": conc_dict["p0"],
                    "Electrons (cm^-3)": conc_dict["n0"],
                }
                if "Dopant" in conc_dict:
                    row["Dopant (cm^-3)"] = conc_dict["Dopant"]
                row.update({"Defect": k, "Concentration (cm^-3)": v})
                data.append(row)

        results_df = pd.DataFrame(data)
        return results_df.set_index("Defect", drop=True)

    def pseudo_equilibrium_solve(
        self,
        chempots: dict[str, float],
        quenched_temperature: float,
        annealing_temperature: float,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        Calculate the self-consistent Fermi level and corresponding
        carrier/defect calculations under pseudo-equilibrium conditions given a
        set of elemental chemical potentials, a quenching temperature, and an
        annealing temperature.

        Typically not intended for direct usage, as the same functionality
        is provided by
        ``DefectThermodynamics.get_quenched_fermi_level_and_concentrations()``.

        'Pseudo-equilibrium' here refers to the use of frozen defect and dilute
        limit approximations under the constraint of charge neutrality (i.e.
        constrained equilibrium). According to the 'frozen defect' approximation,
        we typically expect defect concentrations to reach equilibrium during
        annealing/crystal growth (at elevated temperatures), but `not` upon
        quenching (i.e. at room/operating temperature) where we expect kinetic
        inhibition of defect annhiliation and hence non-equilibrium defect
        concentrations / Fermi level.
        Typically this is approximated by computing the equilibrium Fermi level and
        defect concentrations at the annealing temperature, and then assuming the
        total concentration of each defect is fixed to this value, but that the
        relative populations of defect charge states (and the Fermi level) can
        re-equilibrate at the lower (room) temperature. See discussion in
        https://doi.org/10.1039/D3CS00432E (brief),
        https://doi.org/10.1016/j.cpc.2019.06.017 (detailed) and
        ``doped``/``py-sc-fermi`` tutorials for more information.
        In certain cases (such as Li-ion battery materials or extremely slow charge
        capture/emission), these approximations may have to be adjusted such that some
        defects/charge states are considered fixed and some are allowed to
        re-equilibrate (e.g. highly mobile Li vacancies/interstitials). Modelling
        these specific cases can be achieved using the ``free_defects`` and/or
        ``fix_charge_states`` options, as demonstrated in:
        https://doped.readthedocs.io/en/latest/fermisolver_tutorial.html

        This function works by calculating the self-consistent Fermi level and total
        concentration of each defect at the annealing temperature, then fixing the
        total concentrations to these values and re-calculating the self-consistent
        (constrained equilibrium) Fermi level and relative charge state concentrations
        under this constraint at the quenched/operating temperature. If using the
        ``"py-sc-fermi"`` backend, then you can optionally fix the concentrations of
        individual defect `charge states` (rather than fixing total defect concentrations)
        using ``fix_charge_states=True``, and/or optionally specify ``free_defects`` to
        exclude from high-temperature concentration fixing (e.g. highly mobile defects).

        Note that the bulk DOS calculation should be well-converged with respect to
        k-points for accurate Fermi level predictions!

        The degeneracy/multiplicity factor "g" is an important parameter in the defect
        concentration equation and thus Fermi level calculation (see discussion in
        https://doi.org/10.1039/D2FD00043A and https://doi.org/10.1039/D3CS00432E),
        affecting the final concentration by up to 2 orders of magnitude. This factor
        is taken from the product of the ``defect_entry.defect.multiplicity`` and
        ``defect_entry.degeneracy_factors`` attributes.

        Args:
            chempots (dict[str, float]):
                A dictionary containing the chemical potentials for the elements.
                The keys are element symbols, and the values are their corresponding
                chemical potentials.
            annealing_temperature (float):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to the
                highest temperature during annealing/synthesis of the material (at
                which we assume equilibrium defect concentrations) within the frozen
                defect approach.
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``). Defaults to ``False``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in." Defaults to None.
                # TODO: Allow matching of substring

        Returns: # TODO: Check output format for both backends!
            pd.DataFrame:
                A ``DataFrame`` containing the defect and carrier concentrations
                under pseudo-equilibrium conditions, along with the self-consistent
                Fermi level.
                The columns include:
                    - "Fermi Level": The self-consistent Fermi energy.
                    - "Electrons (cm^-3)": The electron concentration.
                    - "Holes (cm^-3)": The hole concentration.
                    - "Annealing Temperature": The annealing temperature.
                    - "Quenched Temperature": The quenched temperature.
                    - "Dopant (cm^-3)": The dopant concentration, if applicable.
                    - "Defect": The defect type.
                    - "Concentration (cm^-3)": The concentration of the defect in cm^-3.

                Additional columns may include concentrations for specific defects
                and other relevant data.
        """
        if self.backend == "doped":
            (
                fermi_level,
                electrons,
                holes,
                concentrations,
            ) = self.defect_thermodynamics.get_quenched_fermi_level_and_concentrations(  # type: ignore
                bulk_dos=self.bulk_dos,
                chempots=chempots,
                limit=None,
                annealing_temperature=annealing_temperature,
                quenched_temperature=quenched_temperature,
                effective_dopant_concentration=effective_dopant_concentration,
            )
            concentrations = concentrations.drop(
                columns=[
                    "Charge",
                    "Charge State Population",
                    "Concentration (cm^-3)",
                    "Formation Energy (eV)",
                ],
            )

            new_columns = {
                "Fermi Level": fermi_level,
                "Electrons (cm^-3)": electrons,
                "Holes (cm^-3)": holes,
                "Annealing Temperature": annealing_temperature,
                "Quenched Temperature": quenched_temperature,
            }

            for column, value in new_columns.items():
                concentrations[column] = value

            trimmed_concentrations_sub_duplicates = concentrations.drop_duplicates()
            excluded_columns = ["Defect"]
            for column in concentrations.columns.difference(excluded_columns):
                concentrations[column] = concentrations[column].astype(float)

            renamed_concentrations = trimmed_concentrations_sub_duplicates.rename(
                columns={"Total Concentration (cm^-3)": "Concentration (cm^-3)"},
            )
            return renamed_concentrations.set_index("Defect", drop=True)

        # else py-sc-fermi:
        defect_system = self._generate_annealed_defect_system(
            chempots=chempots,
            quenched_temperature=quenched_temperature,
            annealing_temperature=annealing_temperature,
            effective_dopant_concentration=effective_dopant_concentration,
            free_defects=free_defects,
            fix_charge_states=fix_charge_states,
        )

        with np.errstate(all="ignore"):
            conc_dict = defect_system.concentration_dict()

        data = []
        for k, v in conc_dict.items():
            if k not in ["Fermi Energy", "n0", "p0"]:
                row = {
                    "Annealing Temperature": annealing_temperature,
                    "Quenched Temperature": quenched_temperature,
                    "Fermi Level": conc_dict["Fermi Energy"],
                    "Holes (cm^-3)": conc_dict["p0"],
                    "Electrons (cm^-3)": conc_dict["n0"],
                }
                row.update({"Defect": k, "Concentration (cm^-3)": v})
                if "Dopant" in conc_dict:
                    row["Dopant (cm^-3)"] = conc_dict["Dopant"]
                row.update({"Defect": k, "Concentration (cm^-3)": v})
                data.append(row)

        results_df = pd.DataFrame(data)
        return results_df.set_index("Defect", drop=True)

    def scan_temperature(
        self,
        temperature_range: Union[float, list[float]],
        chempots: Optional[dict[str, float]] = None,
        limit: Optional[str] = None,
        annealing_temperature_range: Optional[Union[float, list[float]]] = None,
        quenching_temperature_range: Optional[Union[float, list[float]]] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Scan over a range of temperatures and solve for the defect
        concentrations, carrier concentrations, and Fermi energy at each
        temperature.

        Args:
            temperature_range (Union[float, list[float]]): Temperature range to solve over.
            chempots (Optional[dict[str, float]]): Dictionary of chemical potentials to use for
                calculating the defect formation energies (and thus concentrations and Fermi level).
                This can be a dictionary of chemical potentials for a single limit (limit),
                in the format: {element symbol: chemical potential}. If manually specifying chemical
                potentials this way, you can set the `el_refs` option with the DFT reference energies
                of the elemental phases, in which case it is the formal chemical potentials
                (i.e., relative to the elemental references) that should be given here,
                otherwise the absolute (DFT) chemical potentials should be given.
            limit (Optional[str]): The chemical potential limit for which to determine the equilibrium
                Fermi level. Can be either:
                - `None`, if `chempots` corresponds to a single chemical potential limit - otherwise
                will use the first chemical potential limit in the `chempots` dict.
                - `"X-rich"/"X-poor"` where X is an element in the system, in which case the most
                X-rich/poor limit will be used (e.g., "Li-rich").
                - A key in the `chempots["limits"]` dictionary.
            annealing_temperature_range (Optional[Union[float, list[float]]]): Annealing temperature
                range to solve over. Defaults to None.
            quenching_temperature_range (Optional[Union[float, list[float]]]): Quenching temperature
                range to solve over. Defaults to None.
            processes (int): Number of processes to use for parallelization. Defaults to 1.
            kwargs: Additional keyword arguments (e.g., passing free_defects to a py-sc-fermi solver).

        Raises:
            ValueError: If both annealing and quenching temperature ranges are not specified, or only
            one of them is specified.

        Returns:
            pd.DataFrame: DataFrame containing defect and carrier concentrations.
        """
        # Ensure temperature ranges are lists
        if isinstance(temperature_range, float):
            temperature_range = [temperature_range]
        if annealing_temperature_range is not None and isinstance(annealing_temperature_range, float):
            annealing_temperature_range = [annealing_temperature_range]
        if quenching_temperature_range is not None and isinstance(quenching_temperature_range, float):
            quenching_temperature_range = [quenching_temperature_range]

        if chempots is None and limit is not None:
            chempots = self._get_limits(limit)
        elif chempots is None:
            raise ValueError("You must specify a limit or chempots dictionary.")

        if annealing_temperature_range is not None and quenching_temperature_range is not None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots_pseudo)(
                    chempots=chempots,
                    quenched_temperature=quench_temp,
                    annealing_temperature=anneal_temp,
                    **kwargs,
                )
                for quench_temp, anneal_temp in product(
                    quenching_temperature_range, annealing_temperature_range
                )
            )
            all_data_df = pd.concat(all_data)

        elif annealing_temperature_range is None and quenching_temperature_range is None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots)(
                    chempots=chempots, temperature=temperature, **kwargs
                )
                for temperature in temperature_range
            )
            all_data_df = pd.concat(all_data)

        else:
            raise ValueError(
                "You must specify both annealing and quenching temperature, or just temperature."
            )
        return all_data_df

    def scan_chempots(
        self,
        chempots: list[dict[str, float]],
        temperature: Optional[float] = None,
        annealing_temperature: Optional[float] = None,
        quenching_temperature: Optional[float] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Scan over a range of chemical potentials and solve for the defect
        concentrations and Fermi energy at each set of chemical potentials.

        Args:
            chempots (list[dict[str, float]]): A list of dictionaries where each dictionary
                represents a set of chemical potentials to scan over. The keys are element symbols,
                and the values are their corresponding chemical potentials.
            temperature (Optional[float]): The temperature at which to solve for defect concentrations
                and Fermi energy. If `None`, the method will require both `annealing_temperature`
                and `quenching_temperature` to be specified.
            annealing_temperature (Optional[float]): The temperature to anneal at. If provided,
                `quenching_temperature` must also be specified.
            quenching_temperature (Optional[float]): The temperature to quench to. If provided,
                `annealing_temperature` must also be specified.
            processes (int): The number of processes to use for parallelization. Defaults to 1.
            kwargs: Additional keyword arguments, such as options for specific solvers (e.g., passing
                `free_defects` to a py-sc-fermi solver).

        Raises:
            ValueError: If only one of `annealing_temperature` or `quenching_temperature` is specified,
            or if both are `None` without a specified `temperature`.

        Returns:
            pd.DataFrame: A DataFrame containing defect and carrier concentrations for each set
            of chemical potentials. Each row corresponds to a different set of chemical potentials.
        """
        if annealing_temperature is not None and quenching_temperature is not None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots_pseudo)(
                    chempots=chempots,
                    quenched_temperature=quenching_temperature,
                    annealing_temperature=annealing_temperature,
                    **kwargs,
                )
            )
            all_data_df = pd.concat(all_data)

        elif annealing_temperature is None and quenching_temperature is None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots)(
                    chempots=chempots, temperature=temperature, **kwargs
                )
            )
            all_data_df = pd.concat(all_data)

        else:
            raise ValueError(
                "You must specify both annealing and quenching temperature, or just temperature."
            )

        return all_data_df

    def scan_dopant_concentration(
        self,
        effective_dopant_concentration_range: Union[float, list[float]],
        chempots: Optional[dict[str, float]] = None,
        limit: Optional[str] = None,
        temperature: Optional[float] = None,
        annealing_temperature: Optional[float] = None,
        quenching_temperature: Optional[float] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Calculate the defect concentrations under a range of effective dopant
        concentrations.

        Args:
            effective_dopant_concentration_range (Union[float, list[float]]):
                The range of effective dopant concentrations to explore. This can be a single value
                or a list of values representing different concentrations.
            chempots (Optional[dict[str, float]]): A dictionary of chemical potentials for the elements,
                where the keys are element symbols and the values are the corresponding chemical
                potentials.
            limit (Optional[str]): The chemical potential limit to use for calculations. This can
                specify a particular limit such as "X-rich" or "X-poor", or can be a key from the
                chemical potentials dictionary.
            temperature (Optional[float]): The temperature at which to perform the calculations.
                If not provided, `annealing_temperature` and `quenching_temperature` must be specified.
            annealing_temperature (Optional[float]): The temperature at which the system is annealed.
                Must be specified if `quenching_temperature` is provided.
            quenching_temperature (Optional[float]): The temperature to which the system is quenched.
                Must be specified if `annealing_temperature` is provided.
            processes (int): The number of parallel processes to use for the calculations. Defaults to 1.
            kwargs: Additional keyword arguments that may be passed to the solver, such as
                specific options for defect calculations.

        Raises:
            ValueError: Raised if only one of `annealing_temperature` or `quenching_temperature` is
            provided, or if both are `None` without a specified `temperature`.

        Returns:
            pd.DataFrame: A DataFrame containing the defect and carrier concentrations for each
            effective dopant concentration. Each row represents the concentrations for a different
            dopant concentration.
        """
        if isinstance(effective_dopant_concentration_range, float):
            effective_dopant_concentration_range = [effective_dopant_concentration_range]

        if chempots is None and limit is not None:
            chempots = self._get_limits(limit)
        elif chempots is None:
            raise ValueError("You must specify a limit or chempots dictionary.")

        # Existing logic here, now correctly handling floats and lists
        if annealing_temperature is not None and quenching_temperature is not None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._add_effective_dopant_concentration_and_solve_pseudo)(
                    chempots=chempots,
                    quenched_temperature=quenching_temperature,
                    annealing_temperature=annealing_temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                    **kwargs,
                )
                for effective_dopant_concentration in effective_dopant_concentration_range
            )
            all_data_df = pd.concat(all_data)

        elif annealing_temperature is None and quenching_temperature is None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._add_effective_dopant_concentration_and_solve)(
                    chempots=chempots,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                    **kwargs,
                )
                for effective_dopant_concentration in effective_dopant_concentration_range
            )
            all_data_df = pd.concat(all_data)

        else:
            raise ValueError(
                "You must specify both annealing and quenching temperature, or just temperature."
            )

        return all_data_df

    def interpolate_chempots(
        self,
        n_points: int,
        temperature: Optional[float] = 300.0,
        chempots: Optional[list[dict]] = None,
        limits: Optional[list[str]] = None,
        annealing_temperature: Optional[float] = None,
        quenching_temperature: Optional[float] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Interpolate between two sets of chemical potentials and solve for the
        defect concentrations and Fermi energy at each interpolated point.
        Chemical potentials can be interpolated between two sets of chemical
        potentials or between two limits.

        Args:
            n_points (int): The number of points to generate between chemical potential
                end points.
            temperature (Optional[float]): The temperature to solve at. Defaults to 300.0 K.
            chempots (Optional[list[dict]]): A list containing two dictionaries, each representing
                a set of chemical potentials to interpolate between. If not provided, `limits` must
                be specified.
            limits (Optional[list[str]]): A list containing two strings, each representing a chemical
                potential limit (e.g., "X-rich" or "X-poor") to interpolate between. If not provided,
                `chempots` must be specified.
            annealing_temperature (Optional[float]): The temperature at which the system is annealed.
                Must be specified if `quenching_temperature` is provided.
            quenching_temperature (Optional[float]): The temperature to which the system is quenched.
                Must be specified if `annealing_temperature` is provided.
            processes (int): The number of parallel processes to use for the calculations. Defaults to 1.
            kwargs: Additional keyword arguments that may be passed to the solver, such as specific
                options for defect calculations.

        Raises:
            ValueError: Raised if only one of `annealing_temperature` or `quenching_temperature` is
            provided, or if both are `None` without a specified `temperature`.

        Returns:
            pd.DataFrame: A DataFrame containing the defect and carrier concentrations for each
            interpolated set of chemical potentials. Each row represents the concentrations for
            a different interpolated point.
        """
        if chempots is None and limits is not None:
            chempots_1 = self._get_limits(limits[0])
            chempots_2 = self._get_limits(limits[1])
        elif chempots is not None:
            chempots_1 = chempots[0]
            chempots_2 = chempots[1]

        interpolated_chem_pots = self._get_interpolated_chempots(chempots_1, chempots_2, n_points)
        if annealing_temperature is not None and quenching_temperature is not None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots_pseudo)(
                    chempots=chempots,
                    quenched_temperature=quenching_temperature,
                    annealing_temperature=annealing_temperature,
                    **kwargs,
                )
                for chempots in interpolated_chem_pots
            )
            all_data_df = pd.concat(all_data)

        elif annealing_temperature is None and quenching_temperature is None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots)(
                    chempots=chem_pots, temperature=temperature, **kwargs
                )
                for chem_pots in interpolated_chem_pots
            )
            all_data_df = pd.concat(all_data)

        else:
            raise ValueError(
                "You must specify both annealing and quenching temperature, or just temperature."
            )

        return all_data_df

    def _get_interpolated_chempots(
        self,
        chem_pot_start: dict,
        chem_pot_end: dict,
        n_points: int,
    ) -> list:
        """
        Generate a set of interpolated chemical potentials between two points.

        Args:
            chem_pot_start (dict): A dictionary representing the starting chemical potentials.
                The keys are element symbols and the values are their corresponding chemical potentials.
            chem_pot_end (dict): A dictionary representing the ending chemical potentials.
                The keys are element symbols and the values are their corresponding chemical potentials.
            n_points (int): The number of interpolated points to generate,
                including the start and end points.

        Returns:
            list: A list of dictionaries, where each dictionary contains a set of interpolated chemical
            potentials. The length of the list corresponds to `n_points`, and each dictionary corresponds
            to an interpolated state between the starting and ending chemical potentials.
        """
        return [
            {
                key: chem_pot_start[key] + (chem_pot_end[key] - chem_pot_start[key]) * i / (n_points - 1)
                for key in chem_pot_start
            }
            for i in range(n_points)
        ]

    def _solve_and_append_chempots(
        self, chempots: dict[str, float], temperature: float, **kwargs
    ) -> pd.DataFrame:
        """
        Solve for the defect concentrations at a given temperature and set of
        chemical potentials.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials for the elements.
                The keys are element symbols, and the values are their corresponding chemical potentials.
            temperature (float): The temperature at which to solve for defect concentrations, in Kelvin.
            kwargs: Additional keyword arguments that may be passed to the solver, such as options for
                specific defect calculations or solver configurations.

        Returns:
            pd.DataFrame: A DataFrame containing the calculated defect and carrier concentrations,
            along with the self-consistent Fermi energy. The DataFrame also includes the provided
            chemical potentials as additional columns.
        """
        results_df = self.equilibrium_solve(chempots, temperature, **kwargs)  # type: ignore
        for key, value in chempots.items():
            results_df[key] = value
        return results_df

    def _solve_and_append_chempots_pseudo(
        self,
        chempots: dict[str, float],
        quenched_temperature: float,
        annealing_temperature: float,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Solve for the defect concentrations using a pseudo-equilibrium
        approach, given a range of chemical potentials and temperatures.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials
                for the elements. The keys are element symbols, and the values are their
                corresponding chemical potentials.
            quenched_temperature (float): The temperature at which the system is quenched, in Kelvin.
            annealing_temperature (float): The temperature at which the system is annealed, in Kelvin.
            kwargs: Additional keyword arguments that may be passed to the solver, such as options
                for specific defect calculations or solver configurations.

        Returns:
            pd.DataFrame: A DataFrame containing the calculated defect and carrier concentrations
            using the pseudo-equilibrium approach, along with the provided chemical potentials
            as additional columns.
        """
        results_df = self.pseudo_equilibrium_solve(
            chempots, quenched_temperature, annealing_temperature, **kwargs
        )  # type: ignore
        for key, value in chempots.items():
            results_df[key] = value
        return results_df

    def _add_effective_dopant_concentration_and_solve_pseudo(
        self,
        chempots: dict[str, float],
        quenched_temperature: float,
        annealing_temperature: float,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Solve for the defect concentrations under pseudo-equilibrium
        conditions, including the effect of an effective dopant concentration.

        This method calculates the defect concentrations after annealing at a
        high temperature and quenching to a lower temperature, while incorporating
        the effect of a specified effective dopant concentration.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials
                for the elements. The keys are element symbols, and the values are
                their corresponding chemical potentials.
            quenched_temperature (float): The temperature (in Kelvin) to which the system is quenched.
            annealing_temperature (float): The temperature (in Kelvin) at which the system is annealed.
            kwargs: Additional keyword arguments, including:
                - effective_dopant_concentration (float): The fixed concentration (in cm^-3)
                of an arbitrary dopant or impurity in the material. A positive value corresponds
                to donor doping, while a negative value corresponds to acceptor doping.

        Returns:
            pd.DataFrame: A DataFrame containing the defect and carrier concentrations
            under pseudo-equilibrium conditions, including the self-consistent Fermi energy
            and the effective dopant concentration.

        Raises:
            ValueError: If "effective_dopant_concentration" is not provided in `kwargs`.
        """
        if "effective_dopant_concentration" not in kwargs:
            raise ValueError("You must specify the effective dopant concentration.")
        results_df = self._solve_and_append_chempots_pseudo(
            chempots=chempots,
            quenched_temperature=quenched_temperature,
            annealing_temperature=annealing_temperature,
            **kwargs,
        )
        results_df["Dopant (cm^-3)"] = abs(kwargs["effective_dopant_concentration"])
        return results_df

    def _add_effective_dopant_concentration_and_solve(
        self, chempots: dict[str, float], temperature: float, **kwargs
    ) -> pd.DataFrame:
        """
        Solve for the defect concentrations at a given temperature and set of
        chemical potentials, while considering an effective dopant
        concentration.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials for the elements.
                The keys are element symbols, and the values are their corresponding chemical potentials.
            temperature (float): The temperature at which to solve for defect concentrations, in Kelvin.
            kwargs: Additional keyword arguments, including:
                - effective_dopant_concentration (float): The effective dopant concentration
                in the material, required for the calculation. This should be specified in the keyword
                arguments.

        Raises:
            ValueError: If "effective_dopant_concentration" is not provided in `kwargs`.

        Returns:
            pd.DataFrame: A DataFrame containing the defect and carrier concentrations,
            along with the effective dopant concentration and the self-consistent Fermi energy.
            The DataFrame also includes the provided chemical potentials as additional columns.
        """
        if "effective_dopant_concentration" not in kwargs:
            raise ValueError("You must specify the effective dopant concentration.")
        results_df = self._solve_and_append_chempots(chempots=chempots, temperature=temperature, **kwargs)
        results_df["Dopant (cm^-3)"] = abs(kwargs["effective_dopant_concentration"])
        return results_df

    def scan_chemical_potential_grid(
        self,
        chempots: Optional[dict] = None,
        n_points: Optional[int] = 10,
        temperature: Optional[float] = 300,
        annealing_temperature: Optional[float] = None,
        quenching_temperature: Optional[float] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Given a doped-formatted chemical potential dictionary, generate a
        ChemicalPotentialGrid object and return the Fermi energy solutions at
        the grid points.

        Args:
            chempots (Optional[dict]): A dictionary of chemical potentials to scan.
                If not provided, the default chemical potentials from `self.chempots`
                will be used, if available.
            n_points (Optional[int]): The number of points to generate along each axis
                of the grid. The actual number of grid points may be less, as points
                outside the convex hull are excluded. Defaults to 10.
            temperature (Optional[float]): The temperature at which to solve for the
                Fermi energy. Defaults to 300 K.
            annealing_temperature (Optional[float]): The temperature at which the system
                is annealed. Must be specified if `quenching_temperature` is provided.
            quenching_temperature (Optional[float]): The temperature to which the system
                is quenched. Must be specified if `annealing_temperature` is provided.
            processes (int): The number of parallel processes to use for the calculations.
                Defaults to 1.
            kwargs: Additional keyword arguments that may be passed to the solver, such
                as options for specific defect calculations.

        Raises:
            ValueError: If neither `chempots` nor `self.chempots` is provided, or if both
            `annealing_temperature` and `quenching_temperature` are not specified together.

        Returns:
            pd.DataFrame: A DataFrame containing the Fermi energy solutions at the grid
            points, based on the provided chemical potentials and conditions.
        """
        if chempots is None:
            if self.chempots is None or "limits_wrt_el_refs" not in self.chempots:
                raise ValueError(
                    "self.chempots or self.chempots['limits_wrt_el_refs'] is None or missing."
                )
            chempots = self.chempots

        grid = ChemicalPotentialGrid.from_chempots(chempots).get_grid(n_points)

        if annealing_temperature is not None and quenching_temperature is not None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots_pseudo)(
                    chempots=chempots[1].to_dict(),
                    quenched_temperature=quenching_temperature,
                    annealing_temperature=annealing_temperature,
                    **kwargs,
                )
                for chempots in grid.iterrows()
            )
            all_data_df = pd.concat(all_data)

        elif annealing_temperature is None and quenching_temperature is None:
            all_data = Parallel(n_jobs=processes)(
                delayed(self._solve_and_append_chempots)(
                    chempots=chempots[1].to_dict(), temperature=temperature, **kwargs
                )
                for chempots in grid.iterrows()
            )
            all_data_df = pd.concat(all_data)

        else:
            raise ValueError(
                "You must specify both annealing and quenching temperature, or just temperature."
            )

        return all_data_df

    def min_max_X(
        self,
        target: str,
        min_or_max: str,
        chempots: Optional[dict] = None,
        el_refs: Optional[dict] = None,
        tolerance: float = 0.01,
        n_points: int = 10,
        temperature: float = 300,
        annealing_temperature: Optional[float] = None,
        quenching_temperature: Optional[float] = None,
        processes: int = 1,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Search for the chemical potentials that minimize or maximize a target
        variable, such as electron concentration, within a specified tolerance.

        This function iterates over a grid of chemical potentials and "zooms in" on
        the chemical potential that either minimizes or maximizes the target variable.
        The process continues until the change in the target variable is less than
        the specified tolerance.

        Args:
            target (str): The target variable to minimize or maximize, e.g., "Electrons (cm^-3)".
            min_or_max (str): Specify whether to "minimize" or "maximize" the target variable.
            chempots (Optional[dict]): A dictionary of initial chemical potentials to use.
                If not provided, default potentials from `self.chempots` are used.
            el_refs (Optional[dict]): A dictionary of elemental reference energies used
                for calculating chemical potentials relative to these references.
            tolerance (float): The convergence criterion for the target variable. The search
                stops when the target value change is less than this value. Defaults to 0.01.
            n_points (int): The number of points to generate along each axis of the grid for
                the initial search. Defaults to 10.
            temperature (float): The temperature at which to perform the calculations. Defaults to 300 K.
            annealing_temperature (Optional[float]): The temperature at which the system is annealed.
                Must be specified if `quenching_temperature` is provided.
            quenching_temperature (Optional[float]): The temperature to which the system is quenched.
                Must be specified if `annealing_temperature` is provided.
            processes (int): The number of parallel processes to use for the calculations. Defaults to 1.
            kwargs: Additional keyword arguments that may be passed to the solver, such as options
                for specific defect calculations.

        Returns:
            pd.DataFrame: A DataFrame containing the results of the minimization or maximization process,
            including the optimal chemical potentials and the corresponding values of the target variable.

        Raises:
            ValueError: If neither `chempots` nor `self.chempots` is provided, if both
            `annealing_temperature` and `quenching_temperature` are not specified together,
            or if `min_or_max` is not "minimize" or "maximize".
        """
        chempots, _el_refs = _parse_chempots(chempots or self.chempots, el_refs or self.el_refs)
        assert chempots is not None
        starting_grid = ChemicalPotentialGrid.from_chempots(chempots)
        current_vertices = starting_grid.vertices
        chempots_labels = list(current_vertices.columns)
        previous_value = None

        while True:
            if annealing_temperature is not None and quenching_temperature is not None:
                all_data = Parallel(n_jobs=processes)(
                    delayed(self._solve_and_append_chempots_pseudo)(
                        chempots=chempots[1].to_dict(),
                        quenched_temperature=quenching_temperature,
                        annealing_temperature=annealing_temperature,
                        **kwargs,
                    )
                    for chempots in starting_grid.get_grid(n_points).iterrows()
                )
                results_df = pd.concat(all_data)

            elif annealing_temperature is None and quenching_temperature is None:
                all_data = Parallel(n_jobs=processes)(
                    delayed(self._solve_and_append_chempots)(
                        chempots=chempots[1].to_dict(), temperature=temperature, **kwargs
                    )
                    for chempots in starting_grid.get_grid(n_points).iterrows()
                )
                results_df = pd.concat(all_data)

            else:
                raise ValueError(
                    "You must specify both annealing and quenching temperature, or just temperature."
                )

            # Find chemical potentials value where target is lowest or highest
            if target in results_df.columns:
                if min_or_max == "min":
                    target_chem_pot = results_df[results_df[target] == results_df[target].min()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].min()]
                elif min_or_max == "max":
                    target_chem_pot = results_df[results_df[target] == results_df[target].max()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].max()]
                current_value = (
                    results_df[target].min() if min_or_max == "min" else results_df[target].max()
                )

            else:
                # Filter the DataFrame for the specific defect
                filtered_df = results_df[results_df.index == target]
                # Find the row where "Concentration (cm^-3)" is at its minimum or maximum
                if min_or_max == "min":
                    min_value = filtered_df["Concentration (cm^-3)"].min()
                    target_chem_pot = results_df.loc[results_df["Concentration (cm^-3)"] == min_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chem_pot.iloc[0]).all(axis=1)
                    ]

                elif min_or_max == "max":
                    max_value = filtered_df["Concentration (cm^-3)"].max()
                    target_chem_pot = results_df.loc[results_df["Concentration (cm^-3)"] == max_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chem_pot.iloc[0]).all(axis=1)
                    ]
                current_value = (
                    filtered_df["Concentration (cm^-3)"].min()
                    if min_or_max == "min"
                    else filtered_df["Concentration (cm^-3)"].max()
                )
                # get the

            # Check if the change in the target value is less than the tolerance
            if (
                previous_value is not None
                and abs((current_value - previous_value) / previous_value) < tolerance
            ):
                break
            previous_value = current_value
            target_chem_pot = target_chem_pot.drop_duplicates(ignore_index=True)

            new_vertices = [  # get midpoint between current vertices and target_chem_pot
                (current_vertices + row[1]) / 2 for row in target_chem_pot.iterrows()
            ]
            # Generate a new grid around the target_chem_pot that
            # does not go outside the bounds of the starting grid
            new_vertices_df = pd.DataFrame(new_vertices[0], columns=chempots_labels)
            starting_grid = ChemicalPotentialGrid(new_vertices_df.to_dict("index"))

        return target_dataframe

    def _handle_chempots_for_py_sc_fermi(self, chempots: dict[str, float]) -> dict[str, float]:
        """
        Adjust the provided chemical potentials for use with the py-sc-fermi
        backend.

        This method ensures that the chemical potentials are correctly referenced using
        the elemental reference energies stored in the class.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials
                for the elements. The keys are element symbols, and the values are their
                corresponding chemical potentials.

        Returns:
            dict[str, float]: A dictionary containing the adjusted chemical potentials,
            where each potential is shifted by the corresponding elemental reference energy.

        Raises:
            ValueError: If `self.chempots` or `self.chempots['elemental_refs']` is None, indicating
            that the necessary elemental reference energies are not available.
        """
        if self.chempots is not None and self.chempots.get("elemental_refs") is not None:
            elemental_refs = self.chempots["elemental_refs"]
        else:
            # Handle the case where self.chempots or self.chempots["elemental_refs"] is None
            raise ValueError("self.chempots or self.chempots['elemental_refs'] is None")
        return {k: v + elemental_refs.get(k, 0) for k, v in chempots.items()}

    def _generate_dopant_for_py_sc_fermi(self, effective_dopant_concentration: float) -> "DefectSpecies":
        """
        Generate a dopant defect charge state object.

        This method creates a defect charge state object representing an arbitrary dopant or
        impurity in the material, used to include in the charge neutrality condition and
        analyze the Fermi level/doping response under hypothetical doping conditions.

        Args:
            effective_dopant_concentration (float): The fixed concentration of the dopant or impurity
                in the material, specified in cm^-3. A positive value indicates donor doping (positive
                defect charge state), while a negative value indicates acceptor doping (negative defect
                charge state).

        Returns:
            DefectSpecies: An instance of the `DefectSpecies` class, representing the generated dopant
            with the specified charge state and concentration.

        Raises:
            ValueError: If `effective_dopant_concentration` is zero or if there is an issue with generating
            the dopant.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        if effective_dopant_concentration > 0:
            charge = 1
            effective_dopant_concentration = abs(effective_dopant_concentration) / 1e24 * self.volume
        elif effective_dopant_concentration < 0:
            charge = -1
            effective_dopant_concentration = abs(effective_dopant_concentration) / 1e24 * self.volume
        dopant = self._DefectChargeState(
            charge=charge, fixed_concentration=effective_dopant_concentration, degeneracy=1
        )
        return self._DefectSpecies(nsites=1, charge_states={charge: dopant}, name="Dopant")

    def _generate_defect_system(
        self,
        temperature: float,
        chempots: dict[str, float],
        effective_dopant_concentration: Optional[float] = None,
    ) -> "DefectSystem":
        """
        Generates a DefectSystem object from the DefectThermodynamics and a set
        of chemical potentials.

        This method constructs a `DefectSystem` object, which encompasses all relevant
        defect species and their properties under the given conditions, including
        temperature, chemical potentials, and an optional dopant concentration.

        Args:
            temperature (float): The temperature at which to perform the calculations, in Kelvin.
            chempots (dict[str, float]): A dictionary containing the chemical potentials for the elements.
                The keys are element symbols, and the values are their corresponding chemical potentials.
            effective_dopant_concentration (Optional[float]): The fixed concentration (in cm^-3) of
                an arbitrary dopant or impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping response under hypothetical
                doping conditions. A positive value corresponds to donor doping, while a negative value
                corresponds to acceptor doping. Defaults to None, indicating no extrinsic dopant.

        Returns:
            DefectSystem: An initialized `DefectSystem` object, containing the defect species with their
            charge states, formation energies, and degeneracies, as well as the density of states (DOS),
            volume, and temperature of the system.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        entries = sorted(self.defect_thermodynamics.defect_entries, key=lambda x: x.name)
        labels = {_get_label_and_charge(entry.name)[0] for entry in entries}
        defect_species: dict[str, Any] = {}
        defect_species = {label: {"charge_states": {}, "nsites": None, "name": label} for label in labels}
        chempots = self._handle_chempots_for_py_sc_fermi(chempots)

        for entry in entries:
            label, charge = _get_label_and_charge(entry.name)
            defect_species[label]["nsites"] = entry.defect.multiplicity / self.multiplicity_scaling

            formation_energy = self.defect_thermodynamics.get_formation_energy(
                entry, chempots=chempots, fermi_level=0
            )
            total_degeneracy = np.prod(list(entry.degeneracy_factors.values()))
            defect_species[label]["charge_states"][charge] = {
                "charge": charge,
                "energy": formation_energy,
                "degeneracy": total_degeneracy,
            }

        all_defect_species = [self._DefectSpecies.from_dict(v) for k, v in defect_species.items()]
        if effective_dopant_concentration is not None:
            dopant = self._generate_dopant_for_py_sc_fermi(effective_dopant_concentration)
            all_defect_species.append(dopant)

        return self._DefectSystem(
            defect_species=all_defect_species,
            dos=self.bulk_dos,
            volume=self.volume,
            temperature=temperature,
            convergence_tolerance=1e-20,
        )

    def _generate_annealed_defect_system(
        self,
        chempots: dict[str, float],
        quenched_temperature: float,
        annealing_temperature: float,
        fix_charge_states: bool = False,
        effective_dopant_concentration: Optional[float] = None,
        free_defects: Optional[list[str]] = None,
    ) -> "DefectSystem":
        """
        Generate a py-sc-fermi `DefectSystem` object that has defect
        concentrations fixed to the values determined at a high temperature
        (annealing_temperature), and then set to a lower temperature
        (quenched_temperature).

        This method creates a defect system where defect concentrations are initially
        calculated at an annealing temperature and then "frozen" as the system is cooled
        to a lower quenching temperature. It can optionally fix the concentrations of
        individual defect charge states or allow charge states to vary while keeping
        total defect concentrations fixed.

        Args:
            chempots (dict[str, float]): A dictionary containing the chemical potentials
                for the elements. The keys are element symbols, and the values
                are their corresponding chemical potentials.
            quenched_temperature (float): The lower temperature (in Kelvin) to which
                the system is quenched.
            annealing_temperature (float): The higher temperature (in Kelvin)
                at which the system is annealed to set initial defect concentrations.
            fix_charge_states (bool): Whether to fix the concentrations of individual
                defect charge states (True) or allow charge states to vary while
                keeping total defect concentrations fixed (False). Defaults to False.
            effective_dopant_concentration (Optional[float]): The fixed concentration (in cm^-3)
                of an arbitrary dopant/impurity in the material. A positive value
                indicates donor doping, while a negative value indicates acceptor doping.
                Defaults to None, indicating no extrinsic dopant.
            free_defects (Optional[list[str]]): A list of defects to be excluded from high-temperature
                concentration fixing, useful for highly mobile defects that are not expected
                to be "frozen-in." Defaults to None.

        Returns:
            DefectSystem: A low-temperature defect system (`quenched_temperature`)
            with defect concentrations fixed to high-temperature (`annealing_temperature`) values.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        # Calculate concentrations at initial temperature
        if free_defects is None:
            free_defects = []

        defect_system = self._generate_defect_system(
            chempots=chempots,  # chempots handled in _generate_defect_system()
            temperature=annealing_temperature,
            effective_dopant_concentration=effective_dopant_concentration,
        )
        initial_conc_dict = defect_system.concentration_dict()

        # Exclude the free_defects, carrier concentrations and Fermi energy from fixing
        all_free_defects = ["Fermi Energy", "n0", "p0"]
        all_free_defects.extend(free_defects)

        # Get the fixed concentrations of non-exceptional defects
        decomposed_conc_dict = defect_system.concentration_dict(decomposed=True)
        additional_data = {}
        for k, v in decomposed_conc_dict.items():
            if k not in all_free_defects:
                for k1, v1 in v.items():
                    additional_data[k + "_" + str(k1)] = v1
        initial_conc_dict.update(additional_data)

        fixed_concs = {k: v for k, v in initial_conc_dict.items() if k not in all_free_defects}

        # Apply the fixed concentrations
        for defect_species in defect_system.defect_species:
            if fix_charge_states:
                for k, v in defect_species.charge_states.items():
                    key = f"{defect_species.name}_{int(k)}"
                    if key in list(fixed_concs.keys()):
                        v.fix_concentration(fixed_concs[key] / 1e24 * defect_system.volume)

            elif defect_species.name in fixed_concs:
                defect_species.fix_concentration(
                    fixed_concs[defect_species.name] / 1e24 * defect_system.volume
                )

        target_system = deepcopy(defect_system)
        target_system.temperature = quenched_temperature
        return target_system


class ChemicalPotentialGrid:
    """
    A class to represent a grid of chemical potentials and to perform
    operations such as generating a grid within the convex hull of given
    vertices.

    This class provides methods for handling and manipulating chemical
    potential data, including the creation of a grid that spans a specified
    chemical potential space. It is particularly useful in materials science
    for exploring different chemical environments and their effects on material
    properties.
    """

    def __init__(self, chempots: dict[str, Any]) -> None:
        """
        Initializes the `ChemicalPotentialGrid` with chemical potential data.

        This constructor takes a dictionary of chemical potentials and sets up
        the initial vertices of the grid.

        Args:
            chempots (dict[str, Any]): A dictionary containing chemical potential
                information. The keys are element symbols or other identifiers, and
                the values are the corresponding chemical potentials.
        """
        self.vertices = pd.DataFrame.from_dict(chempots, orient="index")

    def get_grid(self, n_points: Optional[int] = None) -> pd.DataFrame:
        """
        Generates a grid within the convex hull of the vertices and
        interpolates the dependent variable values.

        This method creates a grid of points that spans the chemical potential
        space defined by the vertices. It ensures that the generated points lie
        within the convex hull of the provided vertices and interpolates the
        chemical potential values at these points.

        Args:
            n_points (int): The number of points to generate along each axis of the grid.
                Note that this may not always be the final number of points in the grid,
                as points lying outside the convex hull are excluded.
                Defaults to 100.

        Returns:
            pd.DataFrame: A DataFrame containing the points within the convex hull,
            along with their corresponding interpolated chemical potential values.
            Each row represents a point in the grid with associated chemical
            potential values.
        """
        if n_points is None:
            n_points = 100
        return self.grid_from_dataframe(self.vertices, n_points)

    @classmethod
    def from_chempots(cls, chempots: dict[str, Any]) -> "ChemicalPotentialGrid":
        """
        Initializes the ChemicalPotentialGrid with chemical potential data.

        This class method creates an instance of the `ChemicalPotentialGrid` class
        using a dictionary of chemical potentials. It extracts the relevant data
        from the provided dictionary and initializes the grid with this data.

        Args:
            chempots (dict[str, Any]): A dictionary containing chemical potential
                information. The keys are identifiers for the chemical potential limits,
                and the values provide the corresponding chemical potential values relative
                to elemental references.

        Returns:
            ChemicalPotentialGrid: An instance of the `ChemicalPotentialGrid` class
            initialized with the provided chemical potential data.
        """
        return cls(chempots["limits_wrt_el_refs"])

    @staticmethod
    def grid_from_dataframe(mu_dataframe: pd.DataFrame, n_points: int = 100) -> pd.DataFrame:
        """
        Generates a grid within the convex hull of the vertices.

        This method creates a grid of points within the convex hull defined by the
        input DataFrame's independent variables. It interpolates the values of the
        dependent variable over this grid, ensuring that all generated points lie
        within the convex hull of the given vertices.

        Args:
            mu_dataframe (pd.DataFrame): A DataFrame containing the chemical potential data,
                with the last column representing the dependent variable and the preceding
                columns representing the independent variables.
            n_points (int): The number of points to generate along each axis of the grid.
                Note that this may not always be the final number of points in the grid,
                as points lying outside the convex hull are excluded. Defaults to 100.

        Returns:
            pd.DataFrame: A DataFrame containing the points within the convex hull along with
            their corresponding interpolated values of the dependent variable. Each row
            represents a point in the grid, with the last column containing the interpolated
            dependent variable values.
        """
        # Exclude the dependent variable from the vertices
        dependent_variable = mu_dataframe.columns[-1]
        dependent_var = mu_dataframe[dependent_variable].to_numpy()
        independent_vars = mu_dataframe.drop(columns=dependent_variable)

        # Generate the complex number for grid spacing
        complex_num = complex(0, n_points)

        # Get the convex hull of the vertices
        hull = ConvexHull(independent_vars.values)

        # Create a dense grid that covers the entire range of the vertices
        x_min, y_min = independent_vars.min(axis=0)
        x_max, y_max = independent_vars.max(axis=0)
        grid_x, grid_y = np.mgrid[x_min:x_max:complex_num, y_min:y_max:complex_num]  # type: ignore
        grid_points = np.vstack([grid_x.ravel(), grid_y.ravel()]).T

        # Delaunay triangulation to get points inside the hull
        delaunay = Delaunay(hull.points[hull.vertices])
        inside_hull = delaunay.find_simplex(grid_points) >= 0
        points_inside = grid_points[inside_hull]

        # Interpolate the values to get the dependent chemical potential
        values_inside = griddata(independent_vars.values, dependent_var, points_inside, method="linear")

        # Combine points with their corresponding interpolated values
        grid_with_values = np.hstack((points_inside, values_inside.reshape(-1, 1)))

        # add vertices to the grid
        grid_with_values = np.vstack((grid_with_values, mu_dataframe.to_numpy()))

        return pd.DataFrame(
            grid_with_values,
            columns=[*list(independent_vars.columns), dependent_variable],
        )
