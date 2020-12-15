from collections import defaultdict

import logging
import re
import shutil
import tarfile
from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from tqdm.auto import tqdm
from typing import Dict, Optional, Tuple, Union

from lhotse import load_manifest
from lhotse.audio import Recording, RecordingSet
from lhotse.supervision import SupervisionSegment, SupervisionSet
from lhotse.utils import Pathlike, urlretrieve_progress

LIBRISPEECH = ('dev-clean', 'dev-other', 'test-clean', 'test-other',
               'train-clean-100', 'train-clean-360', 'train-other-500')
MINI_LIBRISPEECH = ('dev-clean-2', 'train-clean-5')


def download_and_untar(
        target_dir: Pathlike = '.',
        dataset_parts: Optional[Tuple[str]] = MINI_LIBRISPEECH,
        force_download: Optional[bool] = False,
        base_url: Optional[str] = 'http://www.openslr.org/resources'
) -> None:
    """
    Downdload and untar the dataset, supporting both LibriSpeech and MiniLibrispeech

    :param target_dir: Pathlike, the path of the dir to storage the dataset.
    :param dataset_parts: dataset part name, e.g. 'train-clean-100', 'train-clean-5', 'dev-clean'
    :param force_download: Bool, if True, download the tars no matter if the tars exist.
    :param base_url: str, the url of the OpenSLR resources.
    """

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for part in tqdm(dataset_parts, desc='Downloading LibriSpeech parts'):
        if part in LIBRISPEECH:
            url = f'{base_url}/12'
        elif part in MINI_LIBRISPEECH:
            url = f'{base_url}/31'
        else:
            logging.warning(f'Invalid dataset part name: {part}')
            continue
        tar_name = f'{part}.tar.gz'
        tar_path = target_dir / tar_name
        if force_download or not tar_path.is_file():
            urlretrieve_progress(f'{url}/{tar_name}', filename=tar_path, desc=f'Downloading {tar_name}')
        part_dir = target_dir / f'LibriSpeech/{part}'
        completed_detector = part_dir / '.completed'
        if not completed_detector.is_file():
            shutil.rmtree(part_dir, ignore_errors=True)
            with tarfile.open(tar_path) as tar:
                tar.extractall(path=target_dir)
                completed_detector.touch()


def prepare_librispeech(
        corpus_dir: Pathlike,
        dataset_parts: Union[str, Tuple[str]] = 'auto',
        output_dir: Optional[Pathlike] = None,
        num_jobs: int = 1
) -> Dict[str, Dict[str, Union[RecordingSet, SupervisionSet]]]:
    """
    Returns the manifests which consist of the Recordings and Supervisions.
    When all the manifests are available in the ``output_dir``, it will simply read and return them.

    :param corpus_dir: Pathlike, the path of the data dir.
    :param dataset_parts: dataset part name, e.g. 'train-clean-100', 'train-clean-5', 'dev-clean'
    :param output_dir: Pathlike, the path where to write the manifests.
    :return: a Dict whose key is the dataset part, and the value is Dicts with the keys 'audio' and 'supervisions'.
    """
    corpus_dir = Path(corpus_dir)
    assert corpus_dir.is_dir(), f'No such directory: {corpus_dir}'

    if dataset_parts == 'auto':
        dataset_parts = (
            set(LIBRISPEECH)
                .union(MINI_LIBRISPEECH)
                .intersection(path.name for path in corpus_dir.glob('*'))
        )
        if not dataset_parts:
            raise ValueError(f"Could not find any of librispeech or mini_librispeech splits in: {corpus_dir}")

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Maybe the manifests already exist: we can read them and save a bit of preparation time.
        maybe_manifests = read_if_cached(dataset_parts=dataset_parts, output_dir=output_dir)
        if maybe_manifests is not None:
            return maybe_manifests

    manifests = defaultdict(dict)
    with ThreadPoolExecutor(num_jobs) as ex:
        for part in tqdm(dataset_parts, desc='Dataset parts'):
            recordings = []
            supervisions = []
            part_path = corpus_dir / part
            futures = []
            for trans_path in part_path.rglob('*.txt'):
                # "trans_path" file contains lines like:
                #
                #   121-121726-0000 ALSO A POPULAR CONTRIVANCE
                #   121-121726-0001 HARANGUE THE TIRESOME PRODUCT OF A TIRELESS TONGUE
                #   121-121726-0002 ANGOR PAIN PAINFUL TO HEAR
                #
                # We will create a separate Recording and SupervisionSegment for those.
                with open(trans_path) as f:
                    for line in f:
                        futures.append(ex.submit(parse_utterance, part_path, line))

            for future in tqdm(futures, desc='Utterances', leave=False):
                result = future.result()
                if result is None:
                    continue
                recording, segment = result
                recordings.append(recording)
                supervisions.append(segment)

            recording_set = RecordingSet.from_recordings(recordings)
            supervision_set = SupervisionSet.from_segments(supervisions)

            if output_dir is not None:
                supervision_set.to_json(output_dir / f'supervisions_{part}.json')
                recording_set.to_json(output_dir / f'recordings_{part}.json')

            manifests[part] = {
                'recordings': recording_set,
                'supervisions': supervision_set
            }

    return manifests


def parse_utterance(
        dataset_split_path: Path,
        line: str,
) -> Optional[Tuple[Recording, SupervisionSegment]]:
    recording_id, text = line.strip().split(maxsplit=1)
    # Create the Recording first
    audio_path = dataset_split_path / Path(recording_id.replace('-', '/')).parent / f'{recording_id}.flac'
    if not audio_path.is_file():
        logging.warning(f'No such file: {audio_path}')
        return None
    recording = Recording.from_file(audio_path, recording_id=recording_id)
    # Then, create the corresponding supervisions
    segment = SupervisionSegment(
        id=recording_id,
        recording_id=recording_id,
        start=0.0,
        duration=recording.duration,
        channel=0,
        language='English',
        speaker=re.sub(r'-.*', r'', recording.id),
        text=text.strip()
    )
    return recording, segment


def read_if_cached(
        dataset_parts: Optional[Tuple[str]],
        output_dir: Optional[Pathlike]
) -> Optional[Dict[str, Dict[str, Union[RecordingSet, SupervisionSet]]]]:
    if output_dir is None:
        return None
    manifests = defaultdict(dict)
    for part in dataset_parts:
        for manifest in ('recordings', 'supervisions'):
            path = output_dir / f'{manifest}_{part}.json'
            if not path.is_file():
                # If one of the manifests is not available, assume we need to read and prepare everything
                # to simplify the rest of the code.
                return None
            manifests[part][manifest] = load_manifest(path)
    return dict(manifests)
