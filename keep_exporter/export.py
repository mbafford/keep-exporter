#!/usr/bin/env python3
import datetime
import mimetypes
import pathlib
from typing import Dict, List, NamedTuple, Optional, Set, Tuple, Union, ValuesView

import click
import click_config_file
import frontmatter
import gkeepapi
from gkeepapi.node import NodeAudio, NodeDrawing, NodeImage
from mdutils.mdutils import MdUtils
from pathvalidate import sanitize_filename

mimetypes.add_type("audio/3gpp", ".3gp")


def login(
    user_email: str, password: Optional[str], token: Optional[str]
) -> gkeepapi.Keep:
    keep = gkeepapi.Keep()
    if token:
        try:
            click.echo("Logging in with token")
            keep.resume(user_email, token)
            print(keep.getMasterToken())
            return keep
        except gkeepapi.exception.LoginException as ex:
            raise click.BadParameter(f"Token login (resume) failed: {str(ex)}")

    if password:
        try:
            click.echo("Logging in with password")
            keep.login(user_email, password)
            return keep
        except gkeepapi.exception.LoginException as ex:
            raise click.BadParameter(f"Password login failed: {str(ex)}")

    raise click.BadParameter(f"Neither password nor token provided to login.")


def all_note_media(
    note: gkeepapi._node.Note,
) -> List[Union[NodeImage, NodeDrawing, NodeAudio]]:
    """
    Returns a filtered list of only "media" blobs associate with the note.
    Currently NodeDrawing, NodeImage, and NodeMedia.
    There are other blob types, but they don't seem actionable as media.
    """
    return note.images + note.drawings + note.audio


def download_media(
    keep: gkeepapi.Keep,
    note: gkeepapi._node.Note,
    mediapath: pathlib.Path,
    skip_existing: bool,
) -> Tuple[List[pathlib.Path], int]:

    note_media = all_note_media(note)
    if not note_media:
        return ([], 0)

    ret = []
    downloaded_media = 0

    for media in note_media:
        meta = media.blob.save()
        # ocr = meta["extracted_text"]  # TODO save ocr as metadata? in markdown or image?

        if meta.get("type", "") == "DRAWING":
            extension = mimetypes.guess_extension(
                meta.get("drawingInfo", {})
                .get("snapshotData", {})
                .get("mimetype", "image/png")
            )  # All drawings seem to be pngs
        elif meta.get("type") == "IMAGE":
            extension = mimetypes.guess_extension(meta.get("mimetype", "image/jpeg"))
            # .jpe just feels weird, but it's my default in testing
            if extension == ".jpe":
                extension = ".jpg"
        else:  # 'AUDIO'
            extension = mimetypes.guess_extension(meta.get("mimetype", "audio/3gpp"))

        # nest media files under folders named by the note's ID
        # this simplifies figuring out the note media files came from
        note_media_path = mediapath / note.id
        note_media_path.mkdir(exist_ok=True)

        media_file = (
            note_media_path / f"{sanitize_filename(media.id,max_len=135)}{extension}"
        )

        # checking size isn't perfect, and drawings don't have a size,
        # but it doesn't seem right to always re-download images that likely
        # haven't changed
        if (
            skip_existing
            and media_file.exists()
            and hasattr(media.blob, "byte_size")
            and media_file.stat().st_size == media.blob.byte_size
        ):
            click.echo(
                f"Media file f{media_file} exists and is same size as in Google Keep. Skipping."
            )
        else:
            print(f"Downloading media {meta.get('type')} {media.id} for note {note.id}")

            url = keep._media_api.get(media)
            media_data = keep._media_api._session.get(url)
            with media_file.open("wb") as f:
                f.write(media_data.content)

            downloaded_media += 1

        ret.append(media_file)

    return (ret, downloaded_media)


def build_frontmatter(note: gkeepapi._node.Note, markdown: str) -> frontmatter.Post:
    metadata = {
        "google_keep_id": note.id,
        "title": note.title,
        "pinned": note.pinned,
        "trashed": note.trashed,
        "deleted": note.deleted,
        "color": note.color.name,
        "type": note.type.name,
        "parent_id": note.parent_id,
        "sort": note.sort,
        "url": note.url,
        "tags": [label.name for label in note.labels.all()],
        "timestamps": {
            "created": note.timestamps.created.timestamp(),
            "edited": note.timestamps.edited.timestamp(),
            "updated": note.timestamps.updated.timestamp(),
        },
    }

    # gkeepapi appears to be treating "0" as a timestamp rather than null. Sometimes the data structure does not have the key at all instead of 0.
    if note.timestamps.trashed and note.timestamps.trashed.year > 1970:
        metadata["timestamps"]["trashed"] = note.timestamps.trashed.timestamp()
    if note.timestamps.deleted and note.timestamps.deleted.year > 1970:
        metadata["timestamps"]["deleted"] = note.timestamps.deleted.timestamp()

    return frontmatter.Post(markdown, handler=None, **metadata)


def build_markdown(note: gkeepapi._node.Note, images: List[pathlib.Path]) -> str:
    doc = MdUtils(
        ""
    )  # mdutils requires a string file name. Since we're not using it to write files, we can ignore that.

    doc.new_header(1, note.title)
    doc.new_header(2, "Note")

    text = note.text
    text = text.replace("☑ ", "- [X] ")
    text = text.replace("☐ ", "- [ ] ")

    doc.new_paragraph(text)

    if note.annotations.links:
        doc.new_line()
        doc.new_line()
        doc.new_header(2, "Links")
        doc.new_list(
            [
                doc.new_inline_link(link=link.url, text=link.title)
                for link in note.annotations.links
            ]
        )

    if images:
        doc.new_line()
        doc.new_header(2, "Attached Media")

        for image in images:
            doc.new_line(doc.new_inline_image("", image.name))

    return doc.file_data_text


LocalMedia = NamedTuple(
    "LocalMedia",
    [
        ("path", pathlib.Path),
        ("google_keep_note_id", str),
        ("google_keep_media_id", str),
    ],
)


class LocalNote:
    def __init__(
        self,
        google_keep_id: str,
        path: Optional[pathlib.Path] = None,
        timestamp_updated: Optional[datetime.datetime] = None,
        local_media: Dict[str, LocalMedia] = None,
    ):
        self.google_keep_id = google_keep_id
        self.path = path
        self.timestamp_updated = timestamp_updated

        if not local_media:
            self.local_media: Dict[str, LocalMedia] = {}
        else:
            self.local_media = local_media


def index_existing_files(directory: pathlib.Path) -> Dict[str, LocalNote]:
    """
    Scans the output folder looking for existing markdown files
    and media files and builds an index by google_keep_id of those files
    using the metadata in the markdown frontmatter and the filenames
    of the media files.
    """
    index: Dict[str, LocalNote] = {}

    keep_notes = 0
    unknown_notes = 0
    errors = 0
    media = 0

    for file in directory.rglob("*"):
        if not file.is_file():
            continue

        # markdown file
        if file.name.endswith(".md"):
            try:
                with open(file, "r") as f:
                    fm = frontmatter.load(f)

                    google_keep_id: str = fm.metadata.get("google_keep_id")
                    if google_keep_id:
                        if google_keep_id in index and index[google_keep_id].path:
                            click.echo(
                                f"Same Google Keep ID {google_keep_id} in multiple files:\n"
                                f"    {file}\n"
                                f"    {index[google_keep_id].path}\n"
                                f"Only the last file will be updated."
                            )

                        keep_notes += 1
                        index.setdefault(google_keep_id, LocalNote(google_keep_id))

                        updated: datetime.datetime = datetime.datetime.fromtimestamp(
                            fm.metadata.get("timestamps", {}).get("updated")
                        )

                        index[google_keep_id].timestamp_updated = updated
                        index[google_keep_id].path = file
                    else:
                        unknown_notes += 1

            except IOError as ex:
                errors = 0
                click.echo(
                    "Unable to open Markdown file [{os.path.join(root, file)}]. Skipping: {str(ex)}",
                    err=True,
                )

        # media file
        else:
            media += 1

            google_keep_id = file.parent.name
            media_id = ".".join(file.name.split(".")[0:2])

            index.setdefault(google_keep_id, LocalNote(google_keep_id))
            index[google_keep_id].local_media[media_id] = LocalMedia(
                file, google_keep_id, media_id
            )

    click.echo(
        f"Indexed local files: {keep_notes} Google Keep notes, {unknown_notes} unknown markdown files, {media} media files, {errors} errors"
    )

    return index


def try_rename_note(note: LocalNote, target_file: pathlib.Path) -> pathlib.Path:
    """
    Attempts to rename an existing note to the new canonical filename,
    but accepts failures to rename. Returns the path the note now exists
    in, either the old path or the new renamed path.
    """
    if not note.path:
        return target_file

    click.echo(f"Renaming [{note.path}] to [{target_file}]")

    try:
        note.path.rename(target_file)
        return target_file
    except Exception as ex:
        click.echo(f"Unable to rename note. Using existing name: %s" % ex, err=True)
        return note.path


def build_note_unique_path(
    notepath: pathlib.Path,
    note: gkeepapi._node.Note,
    date_format: str,
    local_index: Dict[str, LocalNote],
) -> pathlib.Path:
    title = note.title.strip()
    if not len(title):
        title = "untitled"

    date_str = note.timestamps.created.strftime(date_format)
    filename = f'{sanitize_filename(f"{date_str} - " + title,max_len=135)}.md'
    target_path = notepath / filename

    local_note = local_index.get(note.id)
    local_path = local_note.path if local_note else None

    if local_path:
        # if the note filename matches the current filename for that note, then we're good
        if local_path == target_path:
            return local_path

        # if re-naming would result in having to de-dupe the target filename, keep the
        # exising filename - initial pass at fixing this just resulted in bouncing between
        # two different filenames each pass
        if target_path.exists():
            click.echo(
                f"Note {note.id} will not be renamed. Target file [{target_path}] exists."
            )
            return local_path

    # otherwise, if the file already exists avoid overwriting it
    # put the unique note ID and an incrementing index at the end of the filename
    dedupe_index = 1
    while target_path.exists():
        filename = f'{sanitize_filename(f"{date_str} - " + title,max_len=135)}.{note.id}.{dedupe_index}.md'
        target_path = notepath / filename
        dedupe_index += 1

    return target_path


def delete_local_only_files(
    local_index: Dict[str, LocalNote],
    keep_notes: Dict[str, List[gkeepapi.node.Note]],
    delete_local: bool,
) -> Tuple[int, int]:
    """
    Checks the local index for any notes or media that exist only locally
    and were not returned in the Google Keep API call.
    """
    deleted_notes, deleted_media = 0, 0

    local_only_note_ids = set(local_index.keys()).difference(set(keep_notes.keys()))

    if local_only_note_ids:
        if not delete_local:
            click.echo(
                f"{len(local_only_note_ids)} notes exist locally, but not in Google Keep. Add argument [--delete-local] to delete."
            )
        else:
            click.echo(
                f"{len(local_only_note_ids)} notes exist locally, but not in Google Keep. Trashing local files."
            )
            for note_id in local_only_note_ids:
                note_path = local_index[note_id].path
                deleted_notes += 1
                if note_path:
                    click.echo(
                        f"    Deleting unknown local note [{note_id}] file [{local_index[note_id].path}]"
                    )
                    note_path.unlink()

    local_only_media: Set[Tuple[str, str]] = set(
        [
            (local_media.google_keep_note_id, local_media.google_keep_media_id)
            for local_note in local_index.values()
            for local_media in local_note.local_media.values()
            if local_media.google_keep_note_id and local_media.google_keep_media_id
        ]
    )

    notes: ValuesView[gkeepapi._node.Note] = keep_notes.values()
    keep_media: Set[Tuple[str, str]] = set(
        [
            (keep_note.id, keep_media.id)
            for keep_note in notes
            for keep_media in all_note_media(keep_note)
        ]
    )

    local_only_media_ids = local_only_media.difference(keep_media)
    if not local_only_media_ids:
        return (deleted_notes, 0)

    if not delete_local:
        click.echo(
            f"{len(local_only_note_ids)} media files exist locally, but not in Google Keep. Add argument [--delete-local] to delete."
        )
        return (deleted_notes, 0)

    for (note_id, media_id) in local_only_media_ids:
        media = local_index[note_id].local_media[media_id]

        click.echo(
            f"    Deleting media [{media_id}] for note [{note_id}] file [{media.path}]"
        )
        media.path.unlink()
        deleted_media += 1

    return (deleted_notes, deleted_media)


def get_click_supplied_value(ctx: click.core.Context, param_name: str) -> Any:
    """
    Find the value passed to Click through the following priority:

    #1 - a parameter passed on the command line
    #2 - a config value passed through @click_config_file
    #3 - None
    """

    # I didn't find in the docs for Click a simpler way to get the
    # parameter if specified, fall back to the default_map if not, None if neither
    # but this feels like a standard thing that should be built-in

    if param_name in ctx.params:
        return ctx.params[param_name]

    if ctx.default_map:
        return ctx.default_map.get(param_name)

    return None


def token_callback_password_or_token(
    ctx: click.core.Context,
    param: Union[click.core.Option, click.core.Parameter],
    value: Any,
) -> Any:
    """
    On the token param (after password), ensure that either a password
    or token were supplied, and if neither was, prompt for the password.
    """
    if value:
        token = value
    else:
        token = get_click_supplied_value(ctx, "token")
    password = get_click_supplied_value(ctx, "password")

    if not token and not password:
        click.echo("Neither password nor token provided. Prompting for password")
        password = click.prompt("Password", hide_input=True)
        ctx.params["password"] = password
        return None

    return token


@click.command(
    context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]}
)
@click_config_file.configuration_option()
@click.option(
    "--user",
    "-u",
    prompt=True,
    required=True,
    envvar="GKEEP_USER",
    show_envvar=True,
    help="Google account email (prompt if empty)",
)
@click.option(
    "--password",
    "-p",
    envvar="GKEEP_PASSWORD",
    show_envvar=True,
    help="Google account password (prompt if empty). Either this or token is required.",
    hide_input=True,
)
@click.option(
    "--token",
    "-t",
    envvar="GKEEP_TOKEN",
    help="Google account token from prior run. Either this or password is required.",
    callback=token_callback_password_or_token,
)
@click.option(
    "--directory",
    "-d",
    default="./gkeep-export",
    show_default=True,
    help="Output directory for exported notes",
    type=click.Path(file_okay=False, dir_okay=True, writable=True),
)
@click.option(
    "--header/--no-header",
    default=True,
    show_default=True,
    help="Choose to include or exclude the frontmatter header",
)
@click.option(
    "--delete-local/--no-delete-local",
    default=False,
    show_default=True,
    help="Choose to delete or leave as-is any notes that exist locally but not in Google Keep",
)
@click.option(
    "--rename-local/--no-rename-local",
    default=False,
    show_default=True,
    help="Choose to rename or leave as-is any notes that change titles in Google Keep",
)
@click.option(
    "--date-format",
    default="%Y-%m-%d",
    show_default=True,
    help="Date format to use for the prefix of the note filenames. Reflects the created date of the note.",
)
@click.option(
    "--skip-existing-media/--no-skip-existing-media",
    default=True,
    show_default=True,
    help="Skip existing media if it appears unchanged from the local copy.",
)
def main(
    directory: str,
    user: str,
    password: Optional[str],
    token: Optional[str],
    header: bool,
    delete_local: bool,
    rename_local: bool,
    date_format: str,
    skip_existing_media: bool,
):
    """A simple utility to export google keep notes to markdown files with metadata stored as a frontmatter header."""
    notepath = pathlib.Path(directory).resolve()
    mediapath = notepath.joinpath("media/")

    click.echo(f"Notes directory: {notepath}")
    click.echo(f"Media directory: {mediapath}")

    keep = login(user, password, token)

    if not notepath.exists():
        click.echo("Notes directory does not exist, creating.")
        notepath.mkdir(parents=True)

    if not mediapath.exists():
        click.echo("Media directory does not exist, creating.")
        mediapath.mkdir(parents=True)

    click.echo("Indexing local files.")
    local_index = index_existing_files(notepath)

    click.echo("Indexing remote notes.")
    keep_notes = dict([(note.id, note) for note in keep.all()])

    skipped_notes, updated_notes, new_notes = 0, 0, 0
    downloaded_media = 0
    deleted_notes, deleted_media = delete_local_only_files(
        local_index, keep_notes, delete_local
    )

    for note in keep_notes.values():  # type: gkeepapi._node.Note
        local_note = local_index.get(note.id)
        if not local_note:
            click.echo(f"Downloading new note {note.id}")
            new_notes += 1

        target_path = build_note_unique_path(notepath, note, date_format, local_index)

        local_path = local_index.get(note.id, LocalNote(note.id)).path
        if local_path:
            if rename_local and local_path != target_path:
                target_path = try_rename_note(local_index[note.id], target_path)
            else:
                target_path = local_path

        # decide to skip after the rename (due to date format change) has a chance
        if local_note:
            if local_note.timestamp_updated == note.timestamps.updated:
                skipped_notes += 1
                continue
            else:
                updated_notes += 1
                click.echo(f"Updating existing file for note {note.id}")

        images, downloaded = download_media(keep, note, mediapath, skip_existing_media)
        markdown = build_markdown(note, images)

        downloaded_media += downloaded

        with target_path.open("wb+") as f:
            if header:
                fmatter = build_frontmatter(note, markdown)
                frontmatter.dump(fmatter, f)
            else:
                f.write(markdown.encode("utf-8"))

    click.echo("Finished syncing.")
    click.echo(
        f"Notes: {skipped_notes} unchanged, {updated_notes} updated, {new_notes} new, {deleted_notes} deleted"
    )
    click.echo(f"Media: {downloaded_media} downloaded, {deleted_media} deleted")


if __name__ == "__main__":
    main()
