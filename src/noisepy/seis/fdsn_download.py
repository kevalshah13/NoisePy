""" download.py
    Step 0: Download module

   isort:skip_file
"""


import logging
import sys
from typing import Tuple
import obspy
import pyasdf
import os
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from noisepy.seis.io.datatypes import ConfigParameters
from noisepy.seis.io.channelcatalog import CSVChannelCatalog
from noisepy.seis.io.utils import TimeLogger
from . import noise_module
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNNoDataException

logger = logging.getLogger(__name__)
if not sys.warnoptions:
    import warnings

    warnings.simplefilter("ignore")

"""
This script:
    1) downloads sesimic data located in a broad region defined
    by user or using a pre-compiled station list;
    2) cleans up raw traces by removing gaps, instrumental response,
    downsampling and trimming to a day length;
    3) saves data into ASDF format (see Krischer et al., 2016 for
    more details on the data structure);
    4) parallelize the downloading processes with MPI.
    5) avoids downloading data for stations that already have 1 or 3 channels

Authors: Chengxin Jiang (chengxin_jiang@fas.harvard.edu)
         Marine Denolle (mdenolle@uw.edu)

NOTE:
    0. MOST occasions you just need to change parameters followed
    with detailed explanations to run the script.
    1. to avoid segmentation fault later in cross-correlation
    calculations due to too large data in memory, a rough
    estimation of the memory needs is made in the beginning of the
    code. you can reduce the value of inc_hours if memory on your
    machine is not enough to load proposed (x) hours of noise data all at once;
    2. if choose to download stations from an existing CSV files,
    stations with the same name but different channel is regarded as different
    stations (same format as those generated by the S0A);
    3. for unknow reasons, including station location code during
    feteching process sometime result in no-data. Therefore, we recommend
    setting location code to "*" in the request setting (L105 & 134) when it is confirmed
    manually by the users that no stations with same name but different location codes occurs.

Enjoy the NoisePy journey!
"""

#########################################################
################ PARAMETER SECTION ######################
#########################################################

# download parameters

# get rough estimate of memory needs to ensure it now below up in S1
MAX_MEM = 5.0  # maximum memory allowed per core in GB

##################################################
# we expect no parameters need to be changed below


def download(direc: str, prepro_para: ConfigParameters) -> None:
    # Force config validation
    prepro_para = ConfigParameters.model_validate(dict(prepro_para), strict=True)

    # client/data center. see https://docs.obspy.org/packages/obspy.clients.fdsn.html for a list
    client = Client(prepro_para.client_url_key)
    chan_list = prepro_para.channels
    sta_list = prepro_para.stations
    executor = ThreadPoolExecutor()

    tlog = TimeLogger(logger, logging.INFO)
    t_tot = tlog.reset()
    dlist = os.path.join(direc, "station.csv")  # CSV file for station location info
    prepro_para.respdir = os.path.join(
        direc, "../resp"
    )  # directory where resp files are located (required if rm_resp is neither 'no' nor 'inv')
    # time tags
    starttime = obspy.UTCDateTime(prepro_para.start_date)
    endtime = obspy.UTCDateTime(prepro_para.end_date)
    logger.debug(
        "station.list selected [%s] for data from %s to %s with %sh interval"
        % (prepro_para.down_list, starttime, endtime, prepro_para.inc_hours)
    )
    logger.info(
        f"""Download
        From: {starttime}
        To: {endtime}
        Stations: {sta_list}
        Channels: {chan_list}
        """
    )
    ncomp = len(chan_list)

    # prepare station info (existing station list vs. fetching from client)
    if prepro_para.down_list:
        if not os.path.isfile(dlist):
            raise IOError("file %s not exist! double check!" % dlist)

        inv = CSVChannelCatalog(dlist).get_inventory(None, None)
        logger.info(f"Read inventory from {dlist}")

    else:
        # calculate the total number of channels to download
        # loop through specified network, station and channel lists
        bulk_req = []
        for inet in prepro_para.net_list:
            for ista in sta_list:
                for ichan in chan_list:
                    bulk_req.append((inet, ista, "*", ichan, starttime, endtime))

        # gather station info
        inv = client.get_stations_bulk(
            bulk=bulk_req,
            minlatitude=prepro_para.lamin,
            maxlatitude=prepro_para.lamax,
            minlongitude=prepro_para.lomin,
            maxlongitude=prepro_para.lomax,
            level="response",
        )
        logger.info("Fetched inventory")

    sta = []
    net = []
    chan = []
    location = []
    lon = []
    lat = []
    elev = []
    nsta = 0
    for K in inv:
        for tsta in K:
            for ch in tsta.channels:
                net.append(K.code)
                sta.append(tsta.code)
                chan.append(ch.code)
                lon.append(tsta.longitude)
                lat.append(tsta.latitude)
                elev.append(tsta.elevation)
                # sometimes one station has many locations and
                # here we only get the first location
                if tsta[0].location_code:
                    location.append(tsta[0].location_code)
                else:
                    location.append("*")
                nsta += 1
    tlog.log("Getting inventory")
    # rough estimation on memory needs (assume float32 dtype)
    nsec_chunk = prepro_para.inc_hours / 24 * 86400
    nseg_chunk = int(np.floor((nsec_chunk - prepro_para.cc_len) / prepro_para.step)) + 1
    npts_chunk = int(nseg_chunk * prepro_para.cc_len * prepro_para.samp_freq)
    memory_size = nsta * npts_chunk * 4 / 1024**3
    if memory_size > MAX_MEM:
        raise ValueError(
            "Require %5.3fG memory but only %5.3fG provided)! Reduce inc_hours to avoid this issue!"
            % (memory_size, MAX_MEM)
        )

    ########################################################
    # ###############DOWNLOAD SECTION#######################
    ########################################################

    os.makedirs(direc, exist_ok=True)

    # output station list
    if not prepro_para.down_list:
        locs_dict = {
            "network": net,
            "station": sta,
            "channel": chan,
            "latitude": lat,
            "longitude": lon,
            "elevation": elev,
        }
        locs = pd.DataFrame(locs_dict)
        locs.to_csv(os.path.join(direc, "station.csv"), index=False)

    # get MPI variables ready
    all_chunk = noise_module.get_event_list(prepro_para.start_date, prepro_para.end_date, prepro_para.inc_hours)
    if len(all_chunk) < 1:
        raise ValueError("Abort! no data chunk between %s and %s" % (prepro_para.start_date, prepro_para.end_date))
    splits = len(all_chunk) - 1

    # broadcast the variables
    # MPI: loop through each time chunk
    for ick in range(splits):
        starttime = obspy.UTCDateTime(all_chunk[ick])
        endtime = obspy.UTCDateTime(all_chunk[ick + 1])

        # keep a track of the channels already exists
        num_records = np.zeros(nsta, dtype=np.int16)

        # filename of the ASDF file
        ff = os.path.join(direc, all_chunk[ick] + "T" + all_chunk[ick + 1] + ".h5")
        if not os.path.isfile(ff):
            with pyasdf.ASDFDataSet(ff, mpi=False, compression="gzip-3", mode="w") as ds:
                pass
        else:
            with pyasdf.ASDFDataSet(ff, mpi=False, mode="r") as rds:
                alist = rds.waveforms.list()
                for ista in range(nsta):
                    tname = net[ista] + "." + sta[ista]
                    if tname in alist:
                        num_records[ista] = len(rds.waveforms[tname].get_waveform_tags())
                        logger.info(f"Found {num_records[ista]} records for {sta[ista]} in {ff}")

        # appending when file exists
        with pyasdf.ASDFDataSet(ff, mpi=False, compression="gzip-3", mode="a") as ds:
            ds.add_stationxml(inv)
            # loop through each channel
            tasks = []
            for ista in range(nsta):
                # continue when there are alreay data for sta A at day X
                if num_records[ista] == ncomp:
                    logger.info(f"Already have {num_records[ista]} for {sta[ista]}")
                    continue
                task = executor.submit(
                    download_stream,
                    prepro_para,
                    inv,
                    net[ista],
                    sta[ista],
                    chan[ista],
                    location[ista],
                    starttime,
                    endtime,
                    ista,
                )
                tasks.append(task)

            for ready in as_completed(tasks):
                # Use partial waits so we can start saving results to the store
                # while other computations are still running
                ista, tr = ready.result()
                if tr and len(tr):
                    if location[ista] == "*":
                        tlocation = str("00")
                    else:
                        tlocation = location[ista]
                    new_tags = "{0:s}_{1:s}".format(chan[ista].lower(), tlocation.lower())
                    logger.info(f"Downloaded {chan[ista]}/{new_tags}")
                    # above we should change the dag for: net.sta.loc.chan
                    ds.add_waveforms(tr, tag=new_tags)

    tlog.log("Total Download", t_tot)


def download_stream(
    prepro_para: ConfigParameters,
    inv: obspy.Inventory,
    net: str,
    sta: str,
    chan: str,
    location: str,
    starttime: obspy.UTCDateTime,
    endtime: obspy.UTCDateTime,
    index: int,
) -> Tuple[int, obspy.Stream]:
    logger.debug(f"Start download for {sta}.{chan}")
    client = Client(prepro_para.client_url_key, timeout=15)
    retries = 5
    while retries > 0:
        retries -= 1
        try:
            tr = client.get_waveforms(
                network=net,
                station=sta,
                channel=chan,
                location=location,
                starttime=starttime,
                endtime=endtime,
            )
        except FDSNNoDataException:
            logger.warning(f"No data available for {starttime}-{endtime}/{sta}.{chan}")
            return -1, None
        except Exception as e:
            logger.warning(f"{type(e)}/{e} for get_waveforms({sta}.{chan})")
            continue

        logger.debug(f"Got waveforms for {sta}.{chan}")

        # preprocess to clean data
        tr = noise_module.preprocess_raw(
            tr,
            inv,
            prepro_para,
            starttime,
            endtime,
        )
        return index, tr
    logger.warning(f"Could not download data for {starttime}-{endtime}/{sta}.{chan}")
    return -1, None


# Point people to new entry point:
if __name__ == "__main__":
    print("Please see:\n\npython noisepy.py download --help\n")
