"""
Utility functions for generating and parsing configurational coordinate (CC)
diagrams, for potential energy surfaces (PESs), Nudged Elastic Band (NEB), non-
radiative recombination calculations etc.
"""

import os
import warnings
from typing import Optional, Union

import numpy as np
from nonrad import ccd
from pymatgen.analysis.structure_matcher import Structure, StructureMatcher
from pymatgen.util.typing import PathLike


def orient_s2_like_s1(struct1: Structure, struct2: Structure, verbose: bool = False):
    """
    Re-orient ``struct2`` (**without changing the structure**) to match
    ``struct1`` as closely as possible, with matching atomic indices as needed
    for VASP NEB calculations and other structural transformation analyses
    (e.g. CC diagrams via ``nonrad``, ``CarrierCapture.jl`` etc.).

    Uses the ``StructureMatcher.get_s2_like_s1()`` method from ``pymatgen``,
    but extended to ensure the correct atomic indices matching and lattice
    vector definitions.

    If ``verbose=True``, information about the mass-weighted displacement
    (ΔQ in amu^(1/2)Å) between the input and re-oriented structures is printed.
    This is the typical x-axis unit in configurational coordinate diagrams
    (see e.g. 10.1103/PhysRevB.90.075202).

    Args:
        struct1 (Structure): Initial structure
        struct2 (Structure): Final structure
        verbose (bool):
            Print information about the mass-weighted displacement
            (ΔQ in amu^(1/2)Å) between the input and re-oriented structures.
            Default: False

    Returns:
        Structure:
        ``struct2`` re-oriented to match ``struct1`` as closely as possible.
    """
    if abs(struct1.volume - struct2.volume) > 1:
        warnings.warn(
            f"Volumes of the two input structures differ: {struct1.volume} Å³ vs {struct2.volume} Å³. "
            f"In most cases (defect NEB, CC diagrams...) this is not desirable!"
        )
    sm_tight = StructureMatcher(stol=0.2, primitive_cell=False)
    sm_loose = StructureMatcher(stol=0.5, primitive_cell=False)
    sm_looser = StructureMatcher(stol=1, primitive_cell=False)
    sm_loosest = StructureMatcher(stol=5, primitive_cell=False)

    struct2_like_struct1 = (  # allow high stols to account for highly-mismatching/distorted structures
        sm_tight.get_s2_like_s1(struct1, struct2)  # _could_ just use stol=5 from the start, which gives
        or sm_loose.get_s2_like_s1(struct1, struct2)  # same answer, but much slower (as it cycles through
        or sm_looser.get_s2_like_s1(struct1, struct2)  # multiple possible matches)
        or sm_loosest.get_s2_like_s1(struct1, struct2)
    )

    if not struct2_like_struct1:
        raise RuntimeError(
            f"``StructureMatcher.get_s2_like_s1()`` failed. Note that this requires your input structures "
            f"to have matching compositions and similar lattices. Input structures have compositions:\n"
            f"struct1: {struct1.composition}\nstruct2: {struct2.composition}\n"
            f"and lattices:\nstruct1: {struct1.lattice}\nstruct2: {struct2.lattice}"
        )

    # ``get_s2_like_s1`` usually doesn't work as desired due to different (but equivalent) lattice vectors
    # (e.g. a=(010) instead of (100) etc.), so here we ensure the lattice definition is the same:
    struct2_really_like_struct1 = Structure(
        struct1.lattice, struct1.species, struct2_like_struct1.frac_coords
    )

    # we see that this rearranges the structure so the atom indices should now match correctly. This should
    # give a lower dQ as we see here (or the same if the original structures matched perfectly)
    delQ_s1_s2 = ccd.get_dQ(struct1, struct2)
    delQ_s1_s2_like_s1_pmg = ccd.get_dQ(struct1, struct2_like_struct1)
    delQ_s2_like_s1_s2 = ccd.get_dQ(struct2_really_like_struct1, struct2)
    delQ_s1_s2_like_s1 = ccd.get_dQ(struct1, struct2_really_like_struct1)

    if delQ_s1_s2_like_s1 > min(delQ_s1_s2, delQ_s1_s2_like_s1_pmg) + 0.1:  # shouldn't happen!
        warnings.warn(
            f"StructureMatcher.get_s2_like_s1() appears to have failed. The mass-weighted displacement "
            f"(ΔQ in amu^(1/2)Å) for the input structures is:\n"
            f"ΔQ(s1/s2) = {delQ_s1_s2:.2f} amu^(1/2)Å\n"
            f"Using ``StructureMatcher.get_s2_like_s1()`` directly gives:\n"
            f"ΔQ(s1/s2_like_s1_pmg) = {delQ_s1_s2_like_s1_pmg:.2f} amu^(1/2)Å\n"
            f"Then using the re-oriented structure gives:\n"
            f"ΔQ(s1/s2_like_s1_doped) = {delQ_s1_s2_like_s1:.2f} amu^(1/2)Å\n"
            f"which should always be less than or equal to the previous two values... Please report this "
            f"case to the doped developers!"
        )

    if verbose:
        print(f"ΔQ(s1/s2) = {delQ_s1_s2:.2f} amu^(1/2)Å")
        print(f"ΔQ(s2_like_s1/s2) = {delQ_s2_like_s1_s2:.2f} amu^(1/2)Å")
        print(f"ΔQ(s1/s2_like_s1) = {delQ_s1_s2_like_s1:.2f} amu^(1/2)Å")

    return struct2_really_like_struct1


get_s2_like_s1 = orient_s2_like_s1  # alias similar to pymatgen's get_s2_like_s1


def get_path_structures(
    struct1: Structure,
    struct2: Structure,
    n_images: Union[int, list] = 7,
    displacements: Optional[Union[np.ndarray, list[float]]] = None,
    displacements2: Optional[Union[np.ndarray, list[float]]] = None,
) -> Union[dict[str, Structure], tuple[dict[str, Structure], dict[str, Structure]]]:
    """
    Generate a series of interpolated structures along the linear path between
    ``struct1`` and ``struct2``, typically for use in NEB calculations or CC
    diagrams.

    Structures are output as a dictionary with keys corresponding to either
    the index of the interpolated structure (0-indexed; ``00``, ``01`` etc
    as for VASP NEB calculations) or the fractional displacement along the
    interpolation path between structures, and values corresponding to the
    interpolated structure. If ``displacements`` is set (and thus two sets
    of structures are generated), a tuple of such dictionaries is returned.

    Note that for NEB calculations, the the lattice vectors and order of
    sites (atomic indices) must be consistent in both ``struct1`` and
    ``struct2``. This can be ensured by using the ``orient_s2_like_s1()``
    function in ``doped.utils.configurations``, as shown in the ``doped``
    tutorials. This is also desirable for CC diagrams, as the atomic indices
    are assumed to match for many parsing and plotting functions (e.g. in
    ``nonrad`` and ``CarrierCapture.jl``), but is not strictly necessary.

    If only ``n_images`` is set (and ``displacements`` is thus ``None``),
    then only one set of interpolated structures is generated (in other words,
    assuming a standard NEB/PES calculation is being performed). If
    ``displacements`` (and possibly ``displacements2``) is set, then two sets
    of interpolated structures are generated (in other words, assuming a CC /
    non-radiative recombination calculation is being performed, where the two
    sets of structures are to be calculated in separate charge/spin etc states).

    Args:
        struct1 (Structure): Initial structure.
        struct2 (Structure): Final structure.
        n_images (int):
            Number of images to interpolate between ``struct1`` and ``struct2``,
            or a list of fractiional interpolation values (displacements) to use.
            Note that ``n_images`` is ignored if ``displacements`` is set (in
            which case CC / non-radiative recombination calculations are
            assumed, otherwise a standard NEB / PES calculation is assumed).
            Default: 7
        displacements (np.ndarray or list):
            Displacements to use for ``struct1`` along the linear transformation
            path to ``struct2``. If set, then CC / non-radiative recombination
            calculations are assumed, and two sets of interpolated structures
            will be generated. If set and ``displacements2`` is not set,
            then the same set of displacements is used for both sets of
            interpolated structures. Default: ``None``
        displacements2 (np.ndarray or list):
            Displacements to use for ``struct2`` along the linear transformation
            path to ``struct1``. If not set and ``displacements`` is not ``None``,
            then the same set of displacements is used for both sets of
            interpolated structures. Default: ``None``
    """
    neb = displacements is None
    if neb:
        disp_1 = struct1.interpolate(
            struct2, n_images, interpolate_lattices=True, pbc=True, autosort_tol=1.2
        )
        disp_2 = None
        if not isinstance(n_images, (int, float)):
            displacements = n_images  # displacement magnitudes provided instead of n_images

    else:
        disp_1 = struct1.interpolate(
            struct2, displacements or n_images, interpolate_lattices=True, pbc=True, autosort_tol=1.2
        )
        disp_2 = struct2.interpolate(
            struct1,
            displacements2 or displacements or n_images,
            interpolate_lattices=True,
            pbc=True,
            autosort_tol=1.2,
        )

    disp_1_dict: dict[str, Structure] = {}
    disp_2_dict: dict[str, Structure] = {}
    for structs_disps_dict_tuple in [
        (disp_1, displacements, disp_1_dict),
        (disp_2, displacements2 or displacements, disp_2_dict),
    ]:
        structs, disps, disp_dict = structs_disps_dict_tuple
        if structs is None:
            continue  # NEB, only one directory written

        for i, struct in enumerate(structs):
            key = f"0{i}" if displacements is None else f"delQ_{disps[i]}"  # type: ignore
            disp_dict[key] = struct

    return disp_1_dict if neb else disp_1_dict, disp_2_dict


def write_path_structures(
    struct1: Structure,
    struct2: Structure,
    output_dir: Optional[PathLike] = None,
    n_images: Union[int, list] = 7,
    displacements: Optional[Union[np.ndarray, list[float]]] = None,
    displacements2: Optional[Union[np.ndarray, list[float]]] = None,
):
    """
    Generate a series of interpolated structures along the linear path between
    ``struct1`` and ``struct2``, typically for use in NEB calculations or CC
    diagrams, and write to folders.

    Folder names are labelled by the index of the interpolated structure
    (0-indexed; ``00``, ``01`` etc as for VASP NEB calculations) or the
    fractional displacement along the interpolation path between structures
    (e.g. ``delQ_0.0``, ``delQ_0.1``, ``delQ_-0.1`` etc), depending on
    the input ``n_images``/``displacements`` settings.

    Note that for NEB calculations, the the lattice vectors and order of
    sites (atomic indices) must be consistent in both ``struct1`` and
    ``struct2``. This can be ensured by using the ``orient_s2_like_s1()``
    function in ``doped.utils.configurations``, as shown in the ``doped``
    tutorials. This is also desirable for CC diagrams, as the atomic indices
    are assumed to match for many parsing and plotting functions (e.g. in
    ``nonrad`` and ``CarrierCapture.jl``), but is not strictly necessary.

    If only ``n_images`` is set (and ``displacements`` is thus ``None``),
    then only one set of interpolated structures is written (in other words,
    assuming a standard NEB/PES calculation is being performed). If
    ``displacements`` (and possibly ``displacements2``) is set, then two sets
    of interpolated structures are written (in other words, assuming a CC /
    non-radiative recombination calculation is being performed, where the two
    sets of structures are to be calculated in separate charge/spin etc states).

    Args:
        struct1 (Structure): Initial structure.
        struct2 (Structure): Final structure.
        output_dir (PathLike):
            Directory to write the interpolated structures to.
            Defaults to "Configuration_Coordinate" if ``displacements`` is set,
            otherwise "NEB".
        n_images (int):
            Number of images to interpolate between ``struct1`` and ``struct2``,
            or a list of fractiional interpolation values (displacements) to use.
            Note that ``n_images`` is ignored if ``displacements`` is set (in
            which case CC / non-radiative recombination calculations are
            assumed, otherwise a standard NEB / PES calculation is assumed).
            Default: 7
        displacements (np.ndarray or list):
            Displacements to use for ``struct1`` along the linear transformation
            path to ``struct2``. If set, then CC / non-radiative recombination
            calculations are assumed, and two sets of interpolated structures
            will be written to file. If set and ``displacements2`` is not set,
            then the same set of displacements is used for both sets of
            interpolated structures. Default: ``None``
        displacements2 (np.ndarray or list):
            Displacements to use for ``struct2`` along the linear transformation
            path to ``struct1``. If not set and ``displacements`` is not ``None``,
            then the same set of displacements is used for both sets of
            interpolated structures. Default: ``None``
    """
    path_structs = get_path_structures(struct1, struct2, n_images, displacements, displacements2)
    path_struct_dicts = [path_structs] if isinstance(path_structs, dict) else list(path_structs)
    output_dir = output_dir or "Configuration_Coordinate" if displacements is not None else "NEB"

    for i, path_struct_dict in enumerate(path_struct_dicts):
        for folder, struct in path_struct_dict.items():
            PES_dir = f"PES_{i+1}" if len(path_struct_dicts) > 1 else ""
            path_to_folder = f"{output_dir}/{PES_dir}/{folder}"
            os.makedirs(path_to_folder, exist_ok=True)
            struct.to(filename=f"{path_to_folder}/POSCAR", fmt="poscar")


# TODO: Quick tests
# TODO: Show example parsing and plotting in tutorials
# CC PES example:
# displacements = np.linspace(-0.4, 0.4, 9)
#     displacements = np.append(np.array([-1.5, -1.2, -1.0, -0.8, -0.6]), displacements)
#     displacements = np.append(displacements, np.array([0.6, 0.8, 1.0, 1.2, 1.5]))
