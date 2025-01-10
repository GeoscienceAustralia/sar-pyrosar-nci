from typing import Union
import os

import numpy as np
import pyproj
from osgeo import gdal
from shapely.geometry import box
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, array_bounds
from rasterio.warp import calculate_default_transform, reproject
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.windows import Window
from rasterio.crs import CRS
from rasterio.windows import from_bounds

def bounds_from_profile(profile):
    return array_bounds(profile['height'], profile['width'], profile['transform'])

def reproject_raster(src_path: str, out_path: str, crs: int):
    """Reproject a raster to the desired crs

    Args:
        src_path (str): path to src raster
        out_path (str): save path of reproj raster
        crs (int): crs e.g. 3031

    Returns:
        str: save path of reproj raster
    """
    # reproject raster to project crs
    with rasterio.open(src_path) as src:
        src_crs = src.crs
        transform, width, height = calculate_default_transform(
            src_crs, crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()

        # get crs proj 
        crs = pyproj.CRS(f"EPSG:{crs}")

        kwargs.update({
            'crs': crs,
            'transform': transform,
            'width': width,
            'height': height})

        with rasterio.open(out_path, 'w', **kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=crs,
                    resampling=Resampling.nearest)
    return out_path

def expand_raster_to_bounds(
    trg_bounds : tuple, 
    src_path : str = '',
    src_profile = None,
    src_array = None,
    fill_value : float = 0,
    save_path : str = ''):
    """Expand the extent of the input array to the target bounds specified
    by the user.
    Parameters

    Tuple[np.ndarray, dict]:
        (expanded_array, expanded_profile) of data.
    """

    assert src_path or (src_profile and src_array is not None) or src_profile, \
        "Either src_path, src_array and src_profile, or src_profile must be provided."

    if src_path:
        with rasterio.open(src_path) as src:
            src_array = src.read(1)
            src_profile = src.profile
            src_left, src_bottom, src_right, src_top = src.bounds
    else:
        src_bounds = array_bounds(src_profile['height'], src_profile['width'], src_profile['transform'])
        src_left, src_bottom, src_right, src_top = src_bounds
    
    # Define the new bounds
    trg_left, trg_bottom, trg_right, trg_top = trg_bounds
    lon_res = abs(src_profile['transform'].a)  # Pixel width
    lat_res = abs(src_profile['transform'].e)  # Pixel height 

    # determine the number of new pixels in each direction
    new_left_pixels = int(abs(trg_left-src_left)/lon_res)
    new_right_pixels = int(abs(trg_right-src_right)/lon_res)
    new_bottom_pixels = int(abs(trg_bottom-src_bottom)/lat_res)
    new_top_pixels = int(abs(trg_top-src_top)/lat_res)
    
    # adjust the new bounds with even pixel multiples of existing
    new_trg_left = src_left - new_left_pixels*lon_res
    new_trg_right = src_right + new_right_pixels*lon_res
    new_trg_bottom = src_bottom - new_bottom_pixels*lat_res
    new_trg_top = src_top + new_top_pixels*lat_res

    # keep source if they are already greater than the desired bounds
    new_trg_left = src_left if src_left < trg_left else new_trg_left
    new_trg_right = src_right if src_right > trg_right else new_trg_right
    new_trg_bottom = src_bottom if src_bottom < trg_bottom else new_trg_bottom
    new_trg_top = src_top if src_top < trg_top else new_trg_top

    # Calculate the new width and height, should be integer values
    new_width = int((new_trg_right - new_trg_left) / lon_res)
    new_height = int((new_trg_top - new_trg_bottom) / lat_res)

    # Define the new transformation matrix
    transform = from_origin(new_trg_left, new_trg_top, lon_res, lat_res)
    
    # Create a new raster dataset with expanded bounds
    fill_profile = src_profile.copy()
    fill_profile.update({
        'width': new_width,
        'height': new_height,
        'transform': transform
    })
    fill_array = np.full((1, new_height, new_width), fill_value=fill_value, dtype=src_profile['dtype'])
    
    if src_array is not None:
        # if an existing src array (e.g. dem) is provided to expand
        trg_array, trg_profile = merge_arrays_with_geometadata(
            arrays = [src_array, fill_array],
            profiles = [src_profile, fill_profile],
            resampling='bilinear',
            nodata = src_profile['nodata'],
            dtype = src_profile['dtype'],
            method='first',
        ) 
    else:
        # we are not expanding an existing array
        # return the fill array that has been constructed based on the src_profile
        trg_array, trg_profile = fill_array, fill_profile
    if save_path:
        with rasterio.open(save_path, 'w', **trg_profile) as dst:
            dst.write(trg_array)

    return trg_array, trg_profile

def merge_raster_files(paths, output_path, nodata_value=0, return_data=True):
    # Create a virtual raster (in-memory description of the merged DEMs)
    vrt_options = gdal.BuildVRTOptions(srcNodata=nodata_value)
    vrt_path = output_path.replace(".tif", ".vrt")  # Temporary VRT file path
    gdal.BuildVRT(vrt_path, paths, options=vrt_options)

    # Convert the virtual raster to GeoTIFF
    translate_options = gdal.TranslateOptions(noData=nodata_value)
    gdal.Translate(output_path, vrt_path, options=translate_options)

    # Optionally, clean up the temporary VRT file
    os.remove(vrt_path)

    # return the array and profile
    if return_data:
        with rasterio.open(output_path) as src:
                arr_profile = src.profile
                arr = src.read()
        return arr, arr_profile


def merge_arrays_with_geometadata(
    arrays: list[np.ndarray],
    profiles: list[dict],
    resampling: str = 'bilinear',
    nodata: Union[float, int] = np.nan,
    dtype: str = None,
    method: str = 'first',
) -> tuple[np.ndarray, dict]:
    # https://github.com/ACCESS-Cloud-Based-InSAR/dem-stitcher/blob/dev/src/dem_stitcher/merge.py
    n_dim = arrays[0].shape
    if len(n_dim) not in [2, 3]:
        raise ValueError('Currently arrays must be in BIP format' 'i.e. channels x height x width or flat array')
    if len(set([len(arr.shape) for arr in arrays])) != 1:
        raise ValueError('All arrays must have same number of dimensions i.e. 2 or 3')

    if len(n_dim) == 2:
        arrays_input = [arr[np.newaxis, ...] for arr in arrays]
    else:
        arrays_input = arrays

    if (len(arrays)) != (len(profiles)):
        raise ValueError('Length of arrays and profiles needs to be the same')

    memfiles = [MemoryFile() for p in profiles]
    datasets = [mfile.open(**p) for (mfile, p) in zip(memfiles, profiles)]
    [ds.write(arr) for (ds, arr) in zip(datasets, arrays_input)]

    merged_arr, merged_trans = merge(
        datasets, resampling=Resampling[resampling], method=method, nodata=nodata, dtype=dtype
    )

    prof_merged = profiles[0].copy()
    prof_merged['transform'] = merged_trans
    prof_merged['count'] = merged_arr.shape[0]
    prof_merged['height'] = merged_arr.shape[1]
    prof_merged['width'] = merged_arr.shape[2]
    if nodata is not None:
        prof_merged['nodata'] = nodata
    if dtype is not None:
        prof_merged['dtype'] = dtype

    [ds.close() for ds in datasets]
    [mfile.close() for mfile in memfiles]

    return merged_arr, prof_merged

def read_raster_with_bounds(file_path, bounds, buffer_pixels=0):
    """
    Reads a specific region of a raster file defined by bounds and returns the data array and profile.

    Parameters:
        file_path (str): Path to the raster file.
        bounds (tuple): Bounding box (min_x, min_y, max_x, max_y) specifying the region to read.

    Returns:
        tuple: A NumPy array of the raster data in the window and the corresponding profile.
    """
    with rasterio.open(file_path) as src:
        # Get pixel size from the transform
        transform = src.transform
        pixel_size_x = abs(transform.a)  # Pixel size in x-direction
        pixel_size_y = abs(transform.e)  # Pixel size in y-direction

        # Convert buffer in pixels to geographic units
        buffer_x = buffer_pixels * pixel_size_x
        buffer_y = buffer_pixels * pixel_size_y

        # Expand bounds by the buffer
        min_x, min_y, max_x, max_y = bounds
        buffered_bounds = (
            min_x - buffer_x,
            min_y - buffer_y,
            max_x + buffer_x,
            max_y + buffer_y
        )

        # Create a window from the buffered bounds
        window = from_bounds(*buffered_bounds, transform=src.transform)

        # Clip the window to the raster's extent to avoid out-of-bounds errors
        window = window.intersection(src.window(*src.bounds))

        # Read the data within the window
        data = src.read(window=window)

        # Adjust the profile for the window
        profile = src.profile.copy()
        profile.update({
            "height": window.height,
            "width": window.width,
            "transform": src.window_transform(window)
        })

    return data, profile
