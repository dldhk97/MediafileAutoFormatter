import re
from abc import ABCMeta
from typing import List, Optional
from loguru import logger

from src.model.file import File
from src.model.folder import Folder
from src.model.metadata import Metadata, SeasonMetadata
from src.model.structable import Structable
from src.model.token import Token
from src.analyzer.metadata_builder import (
    MetadataBuilder,
    MovieMetadataBuilder,
    TVMetadataBuilder,
)
from src.formatter.subtitle_converter import trunc_suffix_from_file_name
from src.analyzer.error import (
    MediaNotFoundException,
    MediaRootNotFoundException,
    EpisodeIndexNotFoundException,
    EpisodeIndexDuplicatedException,
    SeasonIndexNotFoundException,
)
from src.constants import MediaType, FileType, SeasonAlias
from src.env_configs import EnvConfigs


# TODO: regex 적용 필요
def contains_season_keyword(str: str) -> bool:
    for alias in SeasonAlias.SEASON_ALIASES:
        if alias in str.lower():
            return True
    return False


def _extract_number_from_string(str: str) -> Optional[int]:
    for i in re.finditer(r"[0-9]+", str):
        str_index = i.group()
        if str_index.isnumeric():
            return int(str_index)

    return None


def extract_season_index(folder_name: str) -> Optional[int]:
    if not contains_season_keyword(str=folder_name):
        return None
    return _extract_number_from_string(str=folder_name)


def replace_special_chars(str: str) -> str:
    underscore_replaced = str.replace("_", " ")
    dot_replaced = underscore_replaced.replace(".", " ")
    return dot_replaced


class MediaAnalyzer(metaclass=ABCMeta):
    def __init__(
        self,
        env_configs: EnvConfigs,
    ) -> None:
        raise NotImplementedError

    def analyze(self, root: Folder) -> Metadata:
        raise NotImplementedError


class GeneralMediaAnalyzer(MediaAnalyzer):
    def __init__(self, env_configs: EnvConfigs) -> None:
        self._env_configs = env_configs

    def _get_builder(self):
        raise NotImplementedError

    def analyze(self, root: Folder) -> Metadata:
        builder = self._get_builder()

        self._analyze(builder=builder, root=root)

        return builder.build()

    def _analyze(self, builder: MetadataBuilder, root: Folder) -> None:
        builder.set_root(root=root)
        builder.set_title(root.get_title())

        media_root = self._find_media_root(root=root)
        builder.set_media_root(media_root=media_root)
        builder.set_original_title(original_title=media_root.get_title())

    def _find_media_root(self, root: Folder) -> Folder:
        raise NotImplementedError

    def _get_media_files(self, root: Folder) -> List[File]:
        media_files = []
        for file in root.get_files():
            if file.get_file_type() == FileType.MEDIA:
                media_files.append(file)

        if not media_files:
            for child in root.get_folders():
                media_files.extend(self._get_media_files(child))

        return media_files

    # TODO: folder 내에 여러 자막이 있는 경우, 여러 파일 설정 필요
    def _get_subtitle_file(self, folder: Folder) -> List[Structable]:
        subtitles = []

        for file in folder.get_files():
            if file.get_file_type() in (
                FileType.SUBTITLE,
                FileType.ARCHIVED_SUBTITLE,
            ):
                subtitles.append(file)

        if len(subtitles) == 0:
            logger.warning(
                f"Subtitle must exists in {folder.get_absolute_path()}, but not found."
            )

        return subtitles

    def _analyze_subtitles(self, root: Folder) -> List[Structable]:
        if root.contains_subtitle_file():
            return self._get_subtitle_file(folder=root)

        for elem in root.get_structs():
            if isinstance(elem, Folder):
                if elem.contains_subtitle_file():
                    return self._get_subtitle_file(folder=elem)

        logger.info(f"Subtitle not found : {root.get_absolute_path()}")
        return []


class MovieAnalyzer(GeneralMediaAnalyzer):
    def __init__(self, env_configs: EnvConfigs) -> None:
        super().__init__(env_configs)
        self._media_type = MediaType.MOVIE

    def _get_builder(self):
        return MovieMetadataBuilder(self._media_type)

    def _analyze(self, builder: MovieMetadataBuilder, root: Folder) -> None:
        super()._analyze(builder, root)

        subtitles = self._analyze_subtitles(root=root)
        builder.set_subtitles(subtitles=subtitles)

        media_files = self._get_media_files(root=builder.get_media_root())
        builder.set_media_files(media_files=media_files)

    def _find_media_root(self, root: Folder) -> Folder:
        if root.get_number_of_files_by_type(file_type=FileType.MEDIA) > 0:
            return root

        for folder in root.get_folders():
            if folder.get_number_of_files_by_type(file_type=FileType.MEDIA) > 0:
                return folder

        raise MediaRootNotFoundException


class TVAnalyzer(GeneralMediaAnalyzer):
    def __init__(self, env_configs: EnvConfigs) -> None:
        super().__init__(env_configs)
        self._media_type = MediaType.TV

    def _get_builder(self):
        return TVMetadataBuilder()

    def _analyze(self, builder: TVMetadataBuilder, root: Folder) -> None:
        super()._analyze(builder, root)

        seasons = self._analyze_season(
            builder=builder, media_root=builder.get_media_root(), root=root
        )
        builder.set_seasons(seasons=seasons)

    def _find_media_root(self, root: Folder) -> Folder:
        media_contained_folder = []

        for folder in root.get_folders():
            if folder.get_number_of_files_by_type(file_type=FileType.MEDIA) > 0 or (
                contains_season_keyword(folder.get_title())
            ):
                media_contained_folder.append(folder)

        if len(media_contained_folder) == 1:
            return media_contained_folder[0]

        return root

    def _find_season_folders(self, root: Folder) -> List[Folder]:
        season_folders = []

        for folder in root.get_folders():
            if contains_season_keyword(folder.get_title()):
                season_folders.append(folder)

        return season_folders

    def _analyze_season(
        self, builder: TVMetadataBuilder, media_root: Folder, root: Folder
    ) -> dict[int, SeasonMetadata]:
        seasons = {}

        season_folders = self._find_season_folders(root=root)

        # season folder not found
        if not season_folders:
            subtitles = self._analyze_subtitles(root=root)
            media_files = self._get_media_files(root=media_root)

            # try to extract season index, if none then set as first season
            season_index = extract_season_index(folder_name=media_root.get_title())
            if not season_index:
                season_index = 1

            seasons[season_index] = SeasonMetadata(
                title=builder.get_title(),
                original_title=media_root.get_title(),
                root=root,
                media_root=media_root,
                media_files=media_files,
                subtitles=subtitles,
                season_index=season_index,
                episode_files=self._get_episodes(media_files),
            )
            return seasons

        for season_root in season_folders:
            season_title = season_root.get_title()
            season_index = extract_season_index(folder_name=season_title)

            if season_index is None:
                if len(media_root.get_folders()) > 1:
                    raise SeasonIndexNotFoundException(
                        "Failed to detect season index from season folder name, but multiple season folder exists. Try to rename season foler name."
                    )
                season_index = 1

            season_media_root = self._find_media_root(root=season_root)

            subtitles = self._analyze_subtitles(root=season_media_root)
            media_files = self._get_media_files(root=season_media_root)

            seasons[season_index] = SeasonMetadata(
                title=builder.get_title(),
                original_title=season_root.get_title(),
                root=media_root,
                media_root=season_root,
                media_files=media_files,
                subtitles=subtitles,
                season_index=season_index,
                episode_files=self._get_episodes(media_files),
            )

        return seasons

    def _get_episodes(self, media_files: List[File]) -> dict[int, File]:
        episodes = {}

        sorted(media_files, key=lambda file: file.get_title())

        episode_index_not_found_files = []

        file_name_prefix = self._get_file_name_prefix(files=media_files)

        for media_file in media_files:
            file_title = media_file.get_title()

            try:
                episode_index = self._extract_episode_index_from_file_name(
                    file_name=file_title, prefix=file_name_prefix
                )
            except EpisodeIndexNotFoundException:
                logger.info(f"Episode index not found from {file_title}")
                episode_index_not_found_files.append(media_file)
                continue

            if episodes.get(episode_index, None):
                logger.info(f"Duplicated episode index found from {file_title}")
                episode_index_not_found_files.append(media_file)
                continue

            if episodes.get(episode_index):
                raise EpisodeIndexDuplicatedException("Duplicated episode index found")

            episodes[episode_index] = media_file

        return episodes

    def _get_file_name_prefix(self, files: List[File]) -> str:
        saved_tokens: List[Token] = []

        for media_file in files:
            underscore_replaced = replace_special_chars(str=media_file.get_title())
            splited_tokens = underscore_replaced.split(" ")

            idx = 0
            for str_token in splited_tokens:
                token = Token(idx, str_token)
                self._update_saved_tokens(saved_tokens=saved_tokens, input_token=token)

                idx += 1

        prefix_token_last_index = -1
        for idx in range(len(saved_tokens)):
            if saved_tokens[idx].get_count() <= 1:
                prefix_token_last_index = idx - 1
                break

        if prefix_token_last_index >= 0:
            prefix = ""

            for idx in range(len(saved_tokens)):
                if idx > prefix_token_last_index:
                    break
                prefix += saved_tokens[idx].get_str() + " "

            return prefix

        # if each file's token counts is one, then try to extract number
        logger.warning(f"Failed to found file prefix from file name")
        return ""

    def _update_saved_tokens(self, saved_tokens: List[Token], input_token: Token):
        for saved_token in saved_tokens:
            if saved_token == input_token:
                saved_token.count_up()
                return

        saved_tokens.append(input_token)

    def _extract_episode_index_from_file_name(self, file_name: str, prefix: str) -> int:
        underscore_replaced = replace_special_chars(str=file_name)
        suffix_removed = trunc_suffix_from_file_name(file_name=underscore_replaced)
        prefix_removed = suffix_removed.replace(prefix, "")
        splited = prefix_removed.split(" ")

        if len(splited) > 0:
            str_index = splited[0]

            if str_index.isnumeric():
                return int(str_index)

            # try to split by episode spliter
            index = self._extract_episode_index_from_normalized_form(str=str_index)
            if index:
                return index

            # try to remain number only
            index = _extract_number_from_string(str=str_index)
            if index:
                return index

        raise EpisodeIndexNotFoundException

    def _extract_episode_index_from_normalized_form(self, str: str) -> Optional[int]:
        try:
            for i in re.finditer(r"S[0-9]+E[0-9]+", str):
                str_episode = i.group()
                splited = str_episode.split("E")[1]

                if splited.isnumeric():
                    return int(splited)

        except Exception as e:
            logger.warning(
                f"Failed to extract episode index from normalized form (format : S00E00) : {e}"
            )

        return None
