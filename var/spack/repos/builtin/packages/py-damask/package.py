# Copyright 2013-2021 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


class PyDamask(PythonPackage):
    """Pre- and post-processing tools for DAMASK"""

    homepage = "https://damask3.mpie.de"
    url      = "https://damask3.mpie.de/download/damask-3.0.0.tar.xz"

    maintainers = ['MarDieh']

    version('3.0.0-alpha4', sha256='0bb8bde43b27d852b1fb6e359a7157354544557ad83d87987b03f5d629ce5493')
    version('3.0.0-alpha5', sha256='2d2b10901959c26a5bb5c52327cdafc7943bc1b36b77b515b0371221703249ae')

    depends_on('python@3.7:', type=('build', 'run'))
    depends_on('vtk+python')
    depends_on('py-pandas')
    depends_on('py-scipy')
    depends_on('py-h5py')
    depends_on('py-matplotlib')
    depends_on('py-pyyaml')
    # Force the hdf5+hl variant added by py-h5py to also have +fortan.
    # This makes that hdf5 to use the same variants as damask-grid and damask-mesh:
    depends_on('hdf5+fortran')

    build_directory = 'python'
