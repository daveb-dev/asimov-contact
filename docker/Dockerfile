FROM ghcr.io/fenics/test-env:current-mpich
WORKDIR /tmp/


ADD requirements.txt /tmp/requirements.txt

ARG UFL_BRANCH=main
ARG FFCX_BRANCH=main
ARG BASIX_BRANCH=main
ARG DOLFINX_BRANCH=main
ARG BUILD_TYPE=Release
ARG PETSC_ARCH=linux-gnu-real-32
ARG HDF5_DIR="/usr/local"
ARG HDF5_MPI="ON"
# Can be set to "notebook"
ARG JUPYTER_TYPE="lab" 

# Install python dependencies
RUN pip3 install --upgrade pip 
#RUN HDF5_MPI=${HDF5_MPI} CC=mpicc HDF5_DIR=${HDF5_DIR} pip3 install --no-cache-dir -r requirements.txt

# pyvista dependencies from apt
RUN apt-get -qq update && \
    apt-get -y install libgl1-mesa-dev xvfb && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/tmp/*




# Install basix, ffcx, ufl
RUN git clone https://github.com/FEniCS/basix.git && \
    cd basix && \
    git checkout ${BASIX_BRANCH} && \
    cd .. && \
    cmake -G Ninja -B build-basix -DCMAKE_BUILD_TYPE=${BUILD_TYPE} -S ./basix/cpp/ && \
    cmake --build build-basix --parallel 3 && \
    cmake --install build-basix && \
    BUILD_TYPE=${BUILD_TYPE} pip3 install ./basix/python && \
    python3 -m pip install git+https://github.com/FEniCS/ufl.git@${UFL_BRANCH} && \
    python3 -m pip install git+https://github.com/FEniCS/ffcx.git@${FFCX_BRANCH}

# Install DOLFINX
RUN git clone https://github.com/FEniCS/dolfinx.git && \  
    cd dolfinx && \
    git checkout ${DOLFINX_BRANCH} && \
    cd .. && \
    PETSC_ARCH=${PETSC_ARCH} cmake -G Ninja -DCMAKE_BUILD_TYPE=${DOLFINX_BUILD_TYPE} -B build-dolfinx -S ./dolfinx/cpp/ && \
    cmake --build build-dolfinx && \
    cmake --install build-dolfinx && \
    . /usr/local/lib/dolfinx/dolfinx.conf && \
    PETSC_ARCH=${PETSC_ARCH}  BUILD_TYPE=${BUILD_TYPE} python3 -m pip -v install ./dolfinx/python/

RUN git clone https://github.com/Wells-Group/asimov-contact.git && \
 cmake -G Ninja -DCMAKE_BUILD_TYPE="Debug -DCMAKE_CXX_FLAGS="-Wall -Werror -g -pedantic -Ofast -march=native" -B build-contact -S cpp/ && \
 ninja -C build-contact install && \
 BUILD_TYPE="Debug" python3 -m pip -v install python/ && \
 flake8 python/ && \
 pip3 install mypy && \
 cd python && \
 python3 -m mypy . --exclude=build


ENV DEB_PYTHON_INSTALL_LAYOUT=deb_system
ENV HDF5_MPI="ON"
ENV HDF5_DIR="/usr/local"
ENV PYVISTA_JUPYTER_BACKEND="panel"

# Requirements for pyvista
RUN apt-get update && apt-get install -y libgl1-mesa-glx libxrender1 xvfb nodejs

# Upgrade setuptools and pip
# https://github.com/pypa/setuptools/issues/3269#issuecomment-1254507377
# https://github.com/FEniCS/ffcx/issues/553
RUN python3 -m pip install -U "setuptools<=65.5.1" pip pkgconfig

# Install `h5py`
# https://github.com/hl5py/h5py/issues/2222
RUN python3 -m pip install cython
RUN python3 -m pip install --no-build-isolation --no-binary=h5py h5py

ADD requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt
RUN python3 -m pip cache purge

EXPOSE 8888/tcp

ENV SHELL /bin/bash

RUN jupyter lab build

ENTRYPOINT ["jupyter", "lab", "--ip", "0.0.0.0", "--no-browser", "--allow-root"]
