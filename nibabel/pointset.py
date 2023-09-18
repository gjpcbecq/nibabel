"""Point-set structures

Imaging data are sampled at points in space, and these points
can be described by coordinates.
These structures are designed to enable operations on sets of
points, as opposed to the data sampled at those points.

Abstractly, a point set is any collection of points, but there are
two types that warrant special consideration in the neuroimaging
context: grids and meshes.

A *grid* is a collection of regularly-spaced points. The canonical
examples of grids are the indices of voxels and their affine
projection into a reference space.

A *mesh* is a collection of points and some structure that enables
adjacent points to be identified. A *triangular mesh* in particular
uses triplets of adjacent vertices to describe faces.
"""
from __future__ import annotations

import math
import typing as ty
from dataclasses import dataclass, replace

import numpy as np

from nibabel.casting import able_int_type
from nibabel.fileslice import strided_scalar
from nibabel.spatialimages import SpatialImage

if ty.TYPE_CHECKING:  # pragma: no cover
    from typing_extensions import Self

    _DType = ty.TypeVar('_DType', bound=np.dtype[ty.Any])


class CoordinateArray(ty.Protocol):
    ndim: int
    shape: tuple[int, int]

    @ty.overload
    def __array__(self, dtype: None = ..., /) -> np.ndarray[ty.Any, np.dtype[ty.Any]]:
        ...  # pragma: no cover

    @ty.overload
    def __array__(self, dtype: _DType, /) -> np.ndarray[ty.Any, _DType]:
        ...  # pragma: no cover


@dataclass
class Pointset:
    """A collection of points described by coordinates.

    Parameters
    ----------
    coords : array-like
      2-dimensional array with coordinates as rows
    affine : :class:`numpy.ndarray`
      Affine transform to be applied to coordinates array
    homogeneous : :class:`bool`
      Indicate whether the provided coordinates are homogeneous,
      i.e., homogeneous 3D coordinates have the form ``(x, y, z, 1)``
    """

    coordinates: CoordinateArray
    affine: np.ndarray
    homogeneous: bool = False
    ndim = 2
    __array_priority__ = 99

    def __init__(
        self,
        coordinates: CoordinateArray,
        affine: np.ndarray | None = None,
        homogeneous: bool = False,
    ):
        self.coordinates = coordinates
        self.homogeneous = homogeneous

        if affine is None:
            self.affine = np.eye(self.dim + 1)
        else:
            self.affine = np.asanyarray(affine)

        if self.affine.shape != (self.dim + 1,) * 2:
            raise ValueError(f'Invalid affine for {self.dim}D coordinates:\n{self.affine}')
        if np.any(self.affine[-1, :-1] != 0) or self.affine[-1, -1] != 1:
            raise ValueError(f'Invalid affine matrix:\n{self.affine}')

    @property
    def shape(self) -> tuple[int, int]:
        """The shape of the coordinate array"""
        return self.coordinates.shape

    @property
    def n_coords(self) -> int:
        """Number of coordinates

        Subclasses should override with more efficient implementations.
        """
        return self.coordinates.shape[0]

    @property
    def dim(self) -> int:
        """The dimensionality of the space the coordinates are in"""
        return self.coordinates.shape[1] - self.homogeneous

    def __rmatmul__(self, affine: np.ndarray) -> Self:
        """Apply an affine transformation to the pointset

        This will return a new pointset with an updated affine matrix only.
        """
        return replace(self, affine=np.asanyarray(affine) @ self.affine)

    def _homogeneous_coords(self):
        if self.homogeneous:
            return np.asanyarray(self.coordinates)

        ones = strided_scalar(
            shape=(self.coordinates.shape[0], 1),
            scalar=np.array(1, dtype=self.coordinates.dtype),
        )
        return np.hstack((self.coordinates, ones))

    def get_coords(self, *, as_homogeneous: bool = False):
        """Retrieve the coordinates

        Parameters
        ----------
        as_homogeneous : :class:`bool`
            Return homogeneous coordinates if ``True``, or Cartesian
            coordiantes if ``False``.

        name : :class:`str`
            Select a particular coordinate system if more than one may exist.
            By default, `None` is equivalent to `"world"` and corresponds to
            an RAS+ coordinate system.
        """
        ident = np.allclose(self.affine, np.eye(self.affine.shape[0]))
        if self.homogeneous == as_homogeneous and ident:
            return np.asanyarray(self.coordinates)
        coords = self._homogeneous_coords()
        if not ident:
            coords = (self.affine @ coords.T).T
        if not as_homogeneous:
            coords = coords[:, :-1]
        return coords


class TriangularMesh(Pointset):
    def __init__(self, mesh):
        if isinstance(mesh, tuple) and len(mesh) == 2:
            coords, self._triangles = mesh
        elif hasattr(mesh, 'coords') and hasattr(mesh, 'triangles'):
            coords = mesh.coords
            self._triangles = mesh.triangles
        elif hasattr(mesh, 'get_mesh'):
            coords, self._triangles = mesh.get_mesh()
        else:
            raise ValueError('Cannot interpret input as triangular mesh')
        super().__init__(coords)

    @property
    def n_triangles(self):
        """Number of faces

        Subclasses should override with more efficient implementations.
        """
        return self._triangles.shape[0]

    def get_triangles(self):
        """Mx3 array of indices into coordinate table"""
        return self._triangles

    def get_mesh(self, name=None):
        return self.get_coords(name=name), self.get_triangles()

    def get_names(self):
        """List of surface names that can be passed to
        ``get_{coords,triangles,mesh}``
        """
        raise NotImplementedError


class TriMeshFamily(TriangularMesh):
    def __init__(self, mapping, default=None):
        self._triangles = None
        self._coords = {}
        for name, mesh in dict(mapping).items():
            coords, triangles = TriangularMesh(mesh).get_mesh()
            if self._triangles is None:
                self._triangles = triangles
            self._coords[name] = coords

        if default is None:
            default = next(iter(self._coords))
        self._default = default

    def get_names(self):
        return list(self._coords)

    def get_coords(self, name=None):
        if name is None:
            name = self._default
        return self._coords[name]


class Grid(Pointset):
    r"""A regularly-spaced collection of coordinates

    This class provides factory methods for generating Pointsets from
    :class:`~nibabel.spatialimages.SpatialImage`\s and generating masks
    from coordinate sets.
    """

    @classmethod
    def from_image(cls, spatialimage: SpatialImage) -> Self:
        return cls(coordinates=GridIndices(spatialimage.shape[:3]), affine=spatialimage.affine)

    @classmethod
    def from_mask(cls, mask: SpatialImage) -> Self:
        mask_arr = np.bool_(mask.dataobj)
        return cls(
            coordinates=np.c_[np.nonzero(mask_arr)].astype(able_int_type(mask.shape)),
            affine=mask.affine,
        )

    def to_mask(self, shape=None) -> SpatialImage:
        if shape is None:
            shape = tuple(np.max(self.coordinates, axis=1)[: self.dim])
        mask_arr = np.zeros(shape, dtype='bool')
        mask_arr[np.asanyarray(self.coordinates)[:, : self.dim]] = True
        return SpatialImage(mask_arr, self.affine)


class GridIndices:
    """Class for generating indices just-in-time"""

    __slots__ = ('gridshape', 'dtype', 'shape')
    ndim = 2

    def __init__(self, shape, dtype=None):
        self.gridshape = shape
        self.dtype = dtype or able_int_type(shape)
        self.shape = (math.prod(self.gridshape), len(self.gridshape))

    def __repr__(self):
        return f'<{self.__class__.__name__}{self.gridshape}>'

    def __array__(self, dtype=None):
        if dtype is None:
            dtype = self.dtype

        axes = [np.arange(s, dtype=dtype) for s in self.gridshape]
        return np.reshape(np.meshgrid(*axes, copy=False, indexing='ij'), (len(axes), -1)).T
