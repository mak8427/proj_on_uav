#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#----------------------------------------------------------------------------
# Created By  : Rene HJ Heim
# Created Date: 2022/06/22
# Version      : 1.5
# ---------------------------------------------------------------------------

import pandas as pd
import glob
import os
import logging
from smac_functions import *
from config_object import config
from functools import reduce, partial
import numpy as np
from joblib import Parallel, delayed
from tqdm import tqdm
import pandas as pd
import rasterio as rio

import exiftool
from pathlib import Path, PureWindowsPath

import polars as pl
import numpy as np
import math
from timeit import default_timer as timer
import logging
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("process.log"),
        logging.StreamHandler()
    ]
)

# Define a dictionary for each input source containing paths and settings
sources = [
    {
        'out': config.main_extract_out,               # Output directory for results
        'cam_path': config.main_extract_cam_path,     # Path to camera position file
        'dem_path': config.main_extract_dem_path,     # Path to DEM file
        'ori': config.main_extract_ori,               # Path to directory with original images (containing EXIF data)
        'name': config.main_extract_name,             # Output file name prefix
        'path_list_tag': config.main_extract_path_list_tag  # Path list tag for orthophotos
    }
]



# Loop through each source dictionary to process all provided data
for source in sources:
    exiftool_path = r"C:\Program Files\ExifTool\exiftool.exe"
    # Define function to process each chunk of images in parallel
    def build_database(tuple_chunk):

        iteration = tuple_chunk[0]
        chunk = tuple_chunk[1]
        df_list = []

        # Assign source variables to local variables
        out = source['out']

        cam_path = source['cam_path']

        dem_path = source['dem_path']
        ori = source['ori']

        # Log start of DEM processing
        logging.info(f"Processing DEM for iteration {iteration}")
        start = timer()
        start_DEM_i = timer()


        try:
            with rio.open(dem_path) as dem:
                arr_dem = dem.read(1)  # Read DEM as a numpy array
                transform = dem.transform  # Affine transformation for pixel-to-world coordinates
                dem_crs = dem.crs
                #print(f"DEM CRS: {dem_crs}")

            # Create a meshgrid for all indices
            rows, cols = np.indices(arr_dem.shape)
            rows_flat = rows.flatten()
            cols_flat = cols.flatten()

            # Use Rasterio's vectorized function to get all coordinates at once
            x_coords, y_coords = rio.transform.xy(transform, rows_flat, cols_flat, offset='center')
            end= timer()
            logging.info(f"DEM processing completed for iteration {iteration} in {end - start:.2f} seconds")

        # Flatten the arrays and create a DataFrame
            df_dem = pd.DataFrame({
                "Xw": np.array(x_coords),
                "Yw": np.array(y_coords),
                "elev": arr_dem.ravel()
            })
            #print(df_dem['elev'].value_counts())
        except Exception as e:
            logging.error(f"Error processing DEM: {e}")
            return

        try:
            ori_list = [glob.glob(item + "\\*.tif") for item in ori]
            path_flat = [str(PureWindowsPath(path)) for sublist in ori_list for path in sublist]
        except Exception as e:
            logging.error(f"Error retrieving original images: {e}")
            return

        # Process each image in the current chunk
        for each_ortho in tqdm(chunk):
            try:
                path, file = os.path.split(each_ortho)
                name, _ = os.path.splitext(file)
                logging.info(f"Processing orthophoto {file} for iteration {iteration}")

                # Step 1: Retrieve camera position
                start2 = timer()
                campos = pd.read_csv(cam_path, sep='\t', skiprows=2, header=None)
                campos.columns = ['PhotoID', 'X', 'Y', 'Z', 'Omega', 'Phi', 'Kappa', 'r11', 'r12', 'r13',
                                  'r21', 'r22', 'r23', 'r31', 'r32', 'r33']
                campos1 = campos[campos['PhotoID'].str.match(name)]
                xcam, ycam, zcam = campos1['X'].values[0], campos1['Y'].values[0], campos1['Z'].values[0]
                end2 = timer()
                logging.info(f"Camera position retrieved for {file} in {end2 - start2:.2f} seconds")

                # Step 2: Retrieve solar angles from EXIF
                start3 = timer()
                exifobj = [path for path in path_flat if name in path]
                try:
                    with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                        metadata = et.get_metadata(exifobj[0])
                        metadata =metadata[0]  # For debugging purposes
                        sunelev = float(metadata.get('XMP:SolarElevation', 0)) * (180 / math.pi)
                        saa = float(metadata.get('XMP:SolarAzimuth', 0)) * (180 / math.pi)
                    end3 = timer()
                    #print('Getting SAA and Sun Elevation from ortho EXIF data: ', end3 - start3, 'seconds')
                except Exception as e:
                    logging.error(f"Error processing orthophoto {file}: {e}")

                end3 = timer()
                logging.info(f"Solar angles retrieved for {file} in {end3 - start3:.2f} seconds")

                start4 = timer()
                try:
                    with rio.open(each_ortho) as rst:
                        num_bands = rst.count  # Total number of bands

                        # Read all bands into a 3D numpy array of shape (num_bands, height, width)
                        b_all = rst.read()  # shape: (num_bands, height, width)
                        ortho_crs = rst.crs
                        #print(f"Orthophoto CRS: {ortho_crs}")
                        height, width = rst.height, rst.width
                        #print(f"Raster data shape: {b_all.shape}")

                        # Get indices of all pixels
                        rows, cols = np.indices((height, width))
                        rows = rows.flatten()
                        cols = cols.flatten()

                        # Get the world coordinates for these indices (vectorized)
                        Xw, Yw = rio.transform.xy(rst.transform, rows, cols)

                        # Extract band values at all indices
                        # Shape of band_values: (num_pixels, num_bands)
                        band_values = b_all[:, rows, cols].T

                        # Prepare data for DataFrame
                        data = {
                            'Xw': np.array(Xw),
                            'Yw': np.array(Yw),
                        }
                        for idx in range(num_bands):
                            data[f'band{idx + 1}'] = band_values[:, idx]

                        # Create a single DataFrame with all bands
                        df_allbands = pd.DataFrame(data)

                except Exception as e:
                    logging.error(f"Error processing orthophoto {file}: {e}")
                    return None

                end4 = timer()
                logging.info(f"Orthophoto bands processed for {file} in {end4 - start4:.2f} seconds")


                start5 = timer()

                # Round, drop duplicates, and reset index using Polars
                df_dem = pl.DataFrame(df_dem)
                df_allbands = pl.DataFrame(df_allbands)

                df_dem = df_dem.with_columns([
                    pl.col("Xw").round(3),
                    pl.col("Yw").round(3)
                ]).unique()

                df_dem = df_dem.filter(df_dem["elev"] != -32767.0)

                df_allbands = df_allbands.with_columns([
                    pl.col("Xw").round(3),
                    pl.col("Yw").round(3)
                ]).unique()

                # Merge DataFrames on "Xw" and "Yw"
                dfs = [df_dem, df_allbands]
                df_merged = dfs[0]
                for df in dfs[1:]:
                    df_merged = df_merged.join(df, on=["Xw", "Yw"])

                df_merged = df_merged.unique()

                print(df_merged.shape)

                # Calculate angles
                def calculate_vza(row):
                    return 90 - (np.arctan((zcam - row["elev"]) /
                                           math.sqrt((xcam - row["Xw"])**2 + (ycam - row["Yw"])**2)) * (180 / math.pi))

                df_merged = df_merged.with_columns(
                    pl.struct(["Xw", "Yw", "elev"]).apply(lambda x: calculate_vza(x)).alias("vza")
                )

                df_merged = df_merged.with_columns(
                    pl.when(pl.col("band1") == 65535).then(None).otherwise(pl.col("vza")).alias("vza")
                )

                def calculate_vaa_rad(row):
                    dist = math.sqrt((row["Xw"] - xcam)**2 + (row["Yw"] - ycam)**2)
                    acos_term = math.acos((ycam - row["Yw"]) / dist)
                    return acos_term if row["Xw"] - xcam < 0 else -acos_term



                df_merged = df_merged.with_columns(
                    pl.struct(["Xw", "Yw"]).apply(lambda x: calculate_vaa_rad(x)).alias("vaa_rad")
                )

                df_merged = df_merged.with_columns(
                    ((pl.col("vaa_rad") * (180 / math.pi)) - saa).round(2).alias("vaa")
                )

                df_merged = df_merged.with_columns(
                    pl.when(pl.col("band1") == 65535).then(None).otherwise(pl.col("vaa")).alias("vaa")
                )

                # Insert additional columns
                df_merged = df_merged.with_columns([
                    pl.lit(file).alias("path"),
                    pl.lit(xcam).alias("xcam"),
                    pl.lit(ycam).alias("ycam"),
                    pl.lit(round(sunelev, 2)).alias("sunelev"),
                    pl.lit(round(saa, 2)).alias("saa")
                ])

                print('df_merged')

                # Append to list
                df_list.append(df_merged)

                end5 = timer()
                logging.info(f"Data merging and angle calculations completed for {file} in {end5 - start5:.2f} seconds")


            except Exception as e:
                logging.error(f"Error processing orthophoto {file}: {e}")

        # Save results
        try:
            result = pd.concat(df_list).reset_index(drop=True)
            if not os.path.isdir(out):
                os.makedirs(out)
            result.to_feather(f"{out}\\{source['name']}_{iteration}.feather")
            logging.info(f"Results saved for iteration {iteration}")
        except Exception as e:
            logging.error(f"Error saving results for iteration {iteration}: {e}")
        end_DEM_i=timer()
        logging.info(f"Total time of iteration{iteration}: { end_DEM_i-start_DEM_i:.2f} seconds ")


    # Split orthophoto paths into chunks for parallel processing
    path_list = glob.glob(source['path_list_tag'])
    chunks = np.array_split(path_list, 15)
    logging.info(f"Starting parallel processing with {len(chunks)} chunks")

    # Process each chunk in parallel
    Parallel(n_jobs=1)(delayed(build_database)(i) for i in list(enumerate(chunks)))

