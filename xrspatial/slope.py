from functools import partial
from math import atan

import dask.array as da

import numba as nb
from numba import cuda

import numpy as np


from xarray import DataArray

from xrspatial.utils import ngjit
from xrspatial.utils import has_cuda
from xrspatial.utils import cuda_args

try:
    import cupy
except ImportError:
    class cupy(object):
        ndarray = False


@ngjit
def _horn_slope(data, cellsize_x, cellsize_y):
    out = np.zeros_like(data)
    out[:] = np.nan
    rows, cols = data.shape
    for y in range(1, rows-1):
        for x in range(1, cols-1):
            a = data[y+1, x-1]
            b = data[y+1, x]
            c = data[y+1, x+1]
            d = data[y, x-1]
            f = data[y, x+1]
            g = data[y-1, x-1]
            h = data[y-1, x]
            i = data[y-1, x+1]
            dz_dx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * cellsize_x)
            dz_dy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * cellsize_y)
            p = (dz_dx * dz_dx + dz_dy * dz_dy) ** .5
            out[y, x] = np.arctan(p) * 57.29578
    return out


@cuda.jit(device=True)
def _gpu_slope(arr, cellsize_x, cellsize_y):
    a = arr[2, 0]
    b = arr[2, 1]
    c = arr[2, 2]
    d = arr[1, 0]
    f = arr[1, 2]
    g = arr[0, 0]
    h = arr[0, 1]
    i = arr[0, 2]

    two = nb.int32(2.)  # reducing size to int8 causes wrong results

    dz_dx = ((c + two * f + i) - (a + two * d + g)) / (nb.float32(8.) * cellsize_x[0])
    dz_dy = ((g + two * h + i) - (a + two * b + c)) / (nb.float32(8.) * cellsize_y[0])
    p = (dz_dx * dz_dx + dz_dy * dz_dy) ** nb.float32(.5)
    return atan(p) * nb.float32(57.29578)


@cuda.jit
def _horn_slope_cuda(arr, cellsize_x_arr, cellsize_y_arr, out):
    i, j = cuda.grid(2)
    di = 1
    dj = 1
    if (i-di >= 1 and i+di < out.shape[0] - 1 and 
        j-dj >= 1 and j+dj < out.shape[1] - 1):
        out[i, j] = _gpu_slope(arr[i-di:i+di+1, j-dj:j+dj+1],
                               cellsize_x_arr,
                               cellsize_y_arr)


def _run_dask_numpy(agg, cellsize_x, cellsize_y):
    _func = partial(_horn_slope,
                    cellsize_x=cellsize_x,
                    cellsize_y=cellsize_y)

    out = agg.data.map_overlap(_func,
                                depth=(1, 1),
                                boundary=np.nan,
                                meta=np.array(()))
    return out


def _run_numpy(agg, cellsize_x, cellsize_y):
    out = _horn_slope(agg.data, cellsize_x, cellsize_y)
    return out


def _run_dask_cupy(agg, cellsize_x, cellsize_y):

    raise NotImplementedError('Upstream bug in dask prevents cupy backed arrays')

    _func = partial(_run_cupy,
                    cellsize_x=cellsize_x,
                    cellsize_y=cellsize_y)

    out = agg.data.map_overlap(_func,
                               depth=(1, 1),
                               boundary=np.nan,
                               dtype=cupy.float32,
                               meta=cupy.array(()))
    return out


def _run_cupy(data, cellsize_x, cellsize_y):
    cellsize_x_arr = np.array([float(cellsize_x)], dtype='f4')
    cellsize_y_arr = np.array([float(cellsize_y)], dtype='f4')

    pad_rows = 3 // 2
    pad_cols = 3 // 2
    pad_width = ((pad_rows, pad_rows),
                (pad_cols, pad_cols))

    slope_data = np.pad(data, pad_width=pad_width, mode="reflect")

    griddim, blockdim = cuda_args(slope_data.shape)
    slope_agg = cupy.empty(slope_data.shape, dtype='f4')
    slope_agg[:] = np.nan

    _horn_slope_cuda[griddim, blockdim](slope_data,
                                        cellsize_x_arr,
                                        cellsize_y_arr,
                                        slope_agg)
    out = slope_agg[pad_rows:-pad_rows, pad_cols:-pad_cols]
    return out


def slope(agg, name='slope'):
    """Returns slope of input aggregate in degrees.
    Parameters
    ----------
    agg : DataArray
    name : str - name property of output xr.DataArray

    Returns
    -------
    data: DataArray

    Notes:
    ------
    Algorithm References:
     - http://desktop.arcgis.com/en/arcmap/10.3/tools/spatial-analyst-toolbox/how-slope-works.htm
     - Burrough, P. A., and McDonell, R. A., 1998.
      Principles of Geographical Information Systems
      (Oxford University Press, New York), pp 406
    """

    if not isinstance(agg, DataArray):
        raise TypeError("agg must be instance of DataArray")

    if not agg.attrs.get('res'):
        raise ValueError('input xarray must have `res` attr.')

    # get cellsize out from 'res' attribute
    cellsize = agg.attrs.get('res')
    if isinstance(cellsize, tuple) and len(cellsize) == 2 \
            and isinstance(cellsize[0], (int, float)) \
            and isinstance(cellsize[1], (int, float)):
        cellsize_x, cellsize_y = cellsize
    elif isinstance(cellsize, (int, float)):
        cellsize_x = cellsize
        cellsize_y = cellsize
    else:
        raise ValueError('`res` attr of input xarray must be a numeric'
                         ' or a tuple of numeric values.')

    if isinstance(agg.data, da.Array):

        # dask + cupy case
        is_cupy = type(agg.data._meta).__module__.split('.')[0] == 'cupy'

        if has_cuda() and is_cupy:
            out = _run_dask_cupy(agg, cellsize_x, cellsize_y)

        # dask + numpy case
        else:
            out = _run_dask_numpy(agg, cellsize_x, cellsize_y)

    # cupy case
    elif has_cuda() and isinstance(agg.data, cupy.ndarray):
        out = _run_cupy(agg.data, cellsize_x, cellsize_y)

    # numpy case
    else:
        out = _run_numpy(agg, cellsize_x, cellsize_y)

    return DataArray(out,
                     name=name,
                     coords=agg.coords,
                     dims=agg.dims,
                     attrs=agg.attrs)