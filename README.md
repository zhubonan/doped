# **D**efect **O**riented **P**ython **E**nvironment **D**istribution (`doped`)
This is a (mid-development) Python package for managing solid-state defect calculations,
geared toward VASP. Much of it is a modified version of the excellent [PyCDT](https://bitbucket.org/mbkumar/pycdt).  
See [this link](https://www.sciencedirect.com/science/article/pii/S0010465518300079) for the original PyCDT paper.

Defect formation energy plots are templated from [AIDE](https://github.com/SMTG-UCL/aide) and follow the aesthetics
philosopy of [sumo](https://smtg-ucl.github.io/sumo/), both developed by the dynamic duo Adam Jackson and Alex Ganose.

Example Jupyter notebooks (the `.ipynb` files) are provided in [examples](examples) to show the code functionality and usage.

## Requirements
`doped` requires `pymatgen<2022.8.23` and its dependencies.

## Installation
- Because of breaking changes made to the `pymatgen` defects code in version `2022.8.23`, `doped` requires 
`pymatgen<2022.8.23`, which is installed automatically when installing `doped`. 
However, as discussed briefly below and in the example notebooks, the 
[`ShakeNBreak`](https://shakenbreak.readthedocs.io/en/latest/) approach is highly recommended when calculating 
defects in solids, and this package has been updated to be compatible with the latest version of `pymatgen`.
As such, it is recommended to install `doped` in a virtual python environment as follows:

1. 
```bash
conda create -n doped  # create conda environment named doped
conda activate doped  # activate doped conda environment
pip install doped  # install doped package and dependencies
```
And then use this environment whenever using `doped`.
Instead of `conda` you can also use `venv` to setup virtual environments, 
see [here](https://www.freecodecamp.org/news/how-to-setup-virtual-environments-in-python/) for more.

If you want to use the [example files](examples), 
you should clone the repository and install with `pip install -e .` from the `doped` directory.

2. (If not set) Set the VASP pseudopotential directory in `$HOME/.pmgrc.yaml` as follows::
```bash
  PMG_VASP_PSP_DIR: <Path to VASP pseudopotential top directory>
```
Within your `VASP pseudopotential top directory`, you should have a folder named `POT_GGA_PAW_PBE` which contains the `POTCAR.X(.gz)` files (in this case for PBE `POTCAR`s).

(Necessary to generate `POTCAR` files, auto-determine `INCAR` settings such as `NELECT` for charged defects...)
See [here](https://pymatgen.org/installation.html#potcar-setup) if you have issues with this.

3. (Optional) Set the Materials Project API key in `$HOME/.pmgrc.yaml` as follows::
```bash
  MAPI_KEY: <Your mapi key obtained from www.materialsproject.org>
```
(For pulling structures and comparing properties with the Materials Project database).


## `ShakeNBreak`
As shown in the example notebook, it is highly recommended to use the [`ShakeNBreak`](https://shakenbreak.readthedocs.io/en/latest/) approach when calculating point defects in solids, to ensure you have identified the groundstate structures of your defects. As detailed in the [theory paper](https://arxiv.org/abs/2207.09862), skipping this step can result in drastically incorrect formation energies, transition levels, carrier capture (basically any property associated with defects). This approach is followed in the [doped example notebook](https://github.com/SMTG-UCL/doped/blob/master/dope_Example_Notebook.ipynb), with a more in-depth explanation and tutorial given on the [ShakeNBreak](https://shakenbreak.readthedocs.io/en/latest/) website.

Summary GIF:
![ShakeNBreak Summary](files/SnB_Supercell_Schematic_PES_2sec_Compressed.gif)

`SnB` CLI Usage:
![ShakeNBreak CLI](files/SnB_CLI.gif)


### Developer Installation

1. Download the `doped` source code using the command:
```bash
  git clone https://github.com/SMTG-UCL/doped
```
2.  Navigate to root directory:
```bash
  cd doped
```
3.  Install the code, using the command:
```bash
  pip install -e .
```

## Acknowledgments
`doped` has benefitted from feedback from many users, in particular members of the Walsh and Scanlon research groups who have used / are using it in their work. Direct contributors are listed in the `Contributors` sidebar above; including Seán Kavanagh, Katarina Brlec, Adair Nicolson and Sabrine Hachmioune. Code to efficiently identify defect species from input supercell structures was contributed by Dr Alex Ganose.
