import shutil
import zipfile
import hashlib
import traceback
import os
import tempfile
import requests
import re
import uuid
import logging
import threading
from datetime import datetime, timedelta
from time import sleep
from concurrent.futures import ThreadPoolExecutor
from jinja2 import Environment, FileSystemLoader

from unitypackage_extractor.extractor import extractPackage

try:
    from .fbx_diff import build_fbx_path_list, calculate_fbx_diff
except ImportError:
    from fbx_diff import build_fbx_path_list, calculate_fbx_diff

from shared import *
import booth
import booth_sql
import cloudflare
import llm_summary
from logging_setup import attach_syslog_handler

DRY_RUN = None

# Base directory for per-item working folders (created/recreated each cycle).
WORK_ROOT = './work'

# Setup robust logger
thread_local = threading.local()

# Tracks which users have had their error state reset this cycle (dedup so we
# do not POST /reset_error once per item per cycle).
reset_users_this_cycle = set()
reset_users_lock = threading.Lock()

class ContextFilter(logging.Filter):
    def filter(self, record):
        record.order_num = getattr(thread_local, 'order_num', 'main')
        return True

LOG_FORMAT = '[%(asctime)s] - [%(levelname)s] - [%(order_num)s] - %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

logger = logging.getLogger('BoothChecker')
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

if all(not isinstance(f, ContextFilter) for f in logger.filters):
    logger.addFilter(ContextFilter())

class BoothCrawlError(Exception):
    """Custom exception for BOOTH crawling failures."""
    pass

class ChangelogError(Exception):
    """Raised when changelog generation cannot be trusted (e.g. an archive
    failed to extract/parse), so the item must be retried next cycle instead
    of advancing the version file with a misleading diff."""
    pass

# mark_as
#   - 0: Nothing
#   - 1: Added
#   - 2: Deleted
#   - 3: Changed

# download_short_list 
#   - [download_number]
# download_url_list
#   - [download_number, filename]

def prepare_item_data(item):
    """Unpacks item data from DB into a dictionary."""
    return {
        "order_num": item[0],
        "item_number": str(item[1]),
        "name": item[2],
        "encoding": item[3],
        "number_show": bool(item[4]),
        "changelog_show": bool(item[5]),
        "archive_this": bool(item[6]),
        "gift_item": bool(item[7]),
        "summary_this": bool(item[8]),
        "fbx_only": bool(item[9]),
        "booth_cookie": {"_plaza_session_nktz7u": item[10]},
        "discord_user_id": item[11],
        "discord_channel_id": item[12]
    }

def fetch_booth_data(item_data):
    """Crawls booth.pm and returns download and product info."""
    download_short_list = []
    thumblist = []
    
    if item_data["gift_item"]:
        download_url_list, product_info_list = booth.crawling_gift(
            item_data["order_num"], item_data["booth_cookie"], download_short_list, thumblist
        )
    else:
        download_url_list, product_info_list = booth.crawling(
            item_data["order_num"], [item_data["item_number"]], item_data["booth_cookie"], download_short_list, thumblist
        )
    
    if not download_url_list or not product_info_list:
        error_msg = f'Failed to crawl BOOTH page. The page structure might have changed or the session is invalid.'
        logger.error(error_msg)
        send_error_message(item_data["discord_channel_id"], item_data["discord_user_id"])
        raise BoothCrawlError(error_msg)

    return download_url_list, product_info_list, download_short_list, thumblist

def load_and_compare_version(order_num, download_short_list, fbx_only):
    """Loads version file, ensures structure, and reports whether the download list changed."""
    version_file_path = f'./version/json/{order_num}.json'
    
    # In dry run, if the file doesn't exist, simulate a new item by creating an empty version_json.
    if DRY_RUN and not os.path.exists(version_file_path):
        logger.info('Dry run: version file not found, simulating new item.')
        version_json = {'short-list': [], 'name-list': [], 'files': {}, 'fbx-files': {}}
    else:
        # Original logic for non-dry-run or if file exists in dry run.
        if not os.path.exists(version_file_path):
            logger.info('version file not found, creating one.')
            createVersionFile(version_file_path)

        def _load_json(path):
            with open(path, 'r') as f:
                return simdjson.load(f)

        try:
            version_json = _load_json(version_file_path)
        except ValueError:
            logger.warning('version file corrupted, recreating.')
            if not DRY_RUN:
                createVersionFile(version_file_path)
                version_json = _load_json(version_file_path)
            else:
                # If file is corrupted in dry run, also simulate a new item.
                logger.info('Dry run: version file corrupted, simulating new item.')
                version_json = {'short-list': [], 'name-list': [], 'files': {}, 'fbx-files': {}}

    if 'fbx-files' not in version_json:
        version_json['fbx-files'] = {}
    if 'files' not in version_json:
        version_json['files'] = {}
    if 'name-list' not in version_json:
        version_json['name-list'] = []
    if 'short-list' not in version_json:
        version_json['short-list'] = []

    local_list = version_json.get('short-list', [])

    # 다운로드 ID 집합 전체를 비교한다. 과거에는 길이 + 첫/끝 원소만 비교해
    # 중간 파일만 교체되면 변경을 놓치는 경우가 있었다(순서 변화 자체는 무시).
    has_changed = sorted(local_list) != sorted(download_short_list)

    if not has_changed:
        logger.info('nothing has changed.')
        return None, None, False

    if not download_short_list:
        logger.error('BOOTH no responding, but change was detected.')
        return None, None, False
        
    if has_changed:
        logger.info('something has changed.')

    return version_file_path, version_json, has_changed

def process_files_for_changelog(item_data, download_url_list, local_list, download_dir):
    """Downloads new files and archives them if configured."""
    item_name_list = []
    # Namespace archives per order so concurrent items processed within the
    # same second cannot collide on a shared timestamp folder.
    archive_folder = f'./archive/{item_data["order_num"]}/{strftime_now()}'

    for download_number, filename in download_url_list:
        download_path = os.path.join(download_dir, filename)
        item_name_list.append(filename)

        should_download = item_data["changelog_show"] or item_data["archive_this"] or item_data["fbx_only"]

        if should_download:
            logger.info(f'downloading {download_number} to {download_path}')
            booth.download_item(download_number, download_path, item_data["booth_cookie"])

        if item_data["archive_this"] and download_number not in local_list:
            os.makedirs(archive_folder, exist_ok=True)
            archive_path = os.path.join(archive_folder, filename)
            shutil.copyfile(download_path, archive_path)
    
    return item_name_list

def generate_changelog_and_summary(item_data, download_url_list, version_json, download_dir, process_dir):
    """Generates changelog content and returns metadata.

    Returns:
        tuple: (changelog_html_path, s3_object_url, summary_result, diff_found, new_fbx_records)

    Raises:
        ChangelogError: if any archive fails to extract/parse, so the caller
            can skip notifying and retry next cycle instead of emitting a diff
            that would falsely mark unparsed files as Deleted.
    """
    if item_data["fbx_only"]:
        return generate_fbx_changelog_and_summary(item_data, download_url_list, version_json, download_dir, process_dir)

    saved_prehash = {}
    for local_file in version_json['files'].keys():
        element_mark(version_json['files'][local_file], 2, local_file, saved_prehash)

    for _, filename in download_url_list:
        download_path = os.path.join(download_dir, filename)
        logger.info(f'parsing {filename} structure')
        try:
            process_file_tree(download_path, filename, version_json, item_data["encoding"], [], process_dir)
        except Exception as e:
            logger.error(f'An error occurred while parsing {filename}: {e}')
            logger.debug(traceback.format_exc())
            raise ChangelogError(f'failed to parse {filename}') from e

    path_list = generate_path_info(version_json, saved_prehash)
    diff_found = bool(path_list)
    if not diff_found:
        logger.info('No structural changes detected; skipping changelog generation.')
        return None, None, None, False, None
    
    tree = build_tree(path_list)
    html_list_items = tree_to_html(tree)
    
    file_loader = FileSystemLoader('./templates')
    env = Environment(loader=file_loader)
    changelog_html = env.get_template('changelog.html')

    data = {
        'html_list_items': html_list_items
    }
    output = changelog_html.render(data)

    changelog_filename = uuid.uuid4()

    changelog_html_path = f"changelog/{changelog_filename}.html"

    with open(changelog_html_path, 'w', encoding='utf-8') as html_file:
        html_file.write(output)
    
    summary_result = None
    summary_data = files_list(tree)
    if item_data["summary_this"] and gemini_api_key and summary_data and not DRY_RUN:
        logger.info('Generating summary')
        summary_result = f"{summary.chat(summary_data)}"
        logger.debug(summary_result)
    elif item_data["summary_this"] and gemini_api_key and summary_data and DRY_RUN:
        logger.info('Dry run: Skipping summary generation.')
    
    s3_object_url = None
    if s3_uploader and not DRY_RUN:
        try:
            s3_uploader.upload(changelog_html_path, s3['bucket_name'], changelog_html_path)
            logger.info('Changelog uploaded to S3')
            s3_object_url = f"https://{s3['bucket_access_url']}/{changelog_html_path}"
        except Exception as e:
            logger.error(f'Error occurred while uploading changelog to S3: {e}')
    elif s3_uploader and DRY_RUN:
        logger.info('Dry run: Skipping changelog upload to S3.')

    return changelog_html_path, s3_object_url, summary_result, diff_found, None

def generate_fbx_changelog_and_summary(item_data, download_url_list, version_json, download_dir, process_dir):
    """Generates changelog information for FBX-only tracking.

    Raises:
        ChangelogError: if any archive fails to parse, so the caller retries
            next cycle instead of computing an FBX diff from partial data.
    """
    previous_fbx = version_json.get('fbx-files', {}) or {}
    current_fbx = {}

    for _, filename in download_url_list:
        download_path = os.path.join(download_dir, filename)
        logger.info(f'parsing {filename} structure (FBX only)')
        try:
            process_file_tree(download_path, filename, None, item_data["encoding"], [], process_dir, fbx_only=True, fbx_records=current_fbx)
        except Exception as e:
            logger.error(f'An error occurred while parsing {filename}: {e}')
            logger.debug(traceback.format_exc())
            raise ChangelogError(f'failed to parse {filename}') from e

    added_entries, changed_entries, deleted_entries = calculate_fbx_diff(previous_fbx, current_fbx)

    if not added_entries and not changed_entries and not deleted_entries:
        logger.info('No FBX hash differences detected; skipping changelog generation.')
        return None, None, None, False, current_fbx

    path_list = build_fbx_path_list(added_entries, changed_entries, deleted_entries)
    tree = build_tree(path_list)
    html_list_items = tree_to_html(tree) if item_data["changelog_show"] else ''
    summary_data = files_list(tree)

    changelog_html_path = None
    s3_object_url = None
    summary_result = None

    if item_data["summary_this"] and gemini_api_key and summary_data and not DRY_RUN:
        logger.info('Generating summary')
        summary_result = f"{summary.chat(summary_data)}"
        logger.debug(summary_result)
    elif item_data["summary_this"] and gemini_api_key and summary_data and DRY_RUN:
        logger.info('Dry run: Skipping summary generation.')

    if item_data["changelog_show"]:
        file_loader = FileSystemLoader('./templates')
        env = Environment(loader=file_loader)
        changelog_html = env.get_template('changelog.html')

        data = {
            'html_list_items': html_list_items
        }
        output = changelog_html.render(data)

        changelog_filename = uuid.uuid4()
        changelog_html_path = f"changelog/{changelog_filename}.html"

        with open(changelog_html_path, 'w', encoding='utf-8') as html_file:
            html_file.write(output)

        if s3_uploader and not DRY_RUN:
            try:
                s3_uploader.upload(changelog_html_path, s3['bucket_name'], changelog_html_path)
                logger.info('Changelog uploaded to S3')
                s3_object_url = f"https://{s3['bucket_access_url']}/{changelog_html_path}"
            except Exception as e:
                logger.error(f'Error occurred while uploading changelog to S3: {e}')
        elif s3_uploader and DRY_RUN:
            logger.info('Dry run: Skipping changelog upload to S3.')

    return changelog_html_path, s3_object_url, summary_result, True, current_fbx

def send_discord_notification(item_data, product_info, thumb, local_list_name, item_name_list, changelog_html_path, s3_object_url, summary_result):
    """Sends update notification to Discord.

    Returns True only when delivery is confirmed (every required POST returned
    200). Returns False on any failure so the caller can skip advancing the
    version file and have the notification re-sent on the next cycle.
    """
    if DRY_RUN:
        logger.info('Dry run: Skipping Discord notification.')
        return True

    api_url = f'{discord_api_url}/send_message'
    product_name, product_url = product_info
    author_info = booth.crawling_product(product_url)

    data = {
        'name': item_data["name"] or product_name,
        'url': product_url,
        'thumb': thumb,
        'item_number': item_data["item_number"],
        'local_version_list': '\n'.join(local_list_name or []),
        'download_short_list': '\n'.join(item_name_list),
        'author_info': author_info,
        'number_show': item_data["number_show"],
        'changelog_show': item_data["changelog_show"],
        'channel_id': item_data["discord_channel_id"],
        's3_object_url': s3_object_url,
        'summary': summary_result,
    }

    try:
        response = requests.post(api_url, json=data, timeout=30)
    except requests.RequestException as e:
        logger.error(f'send_message API 요청 실패: {e}')
        return False

    if response.status_code == 200:
        logger.info('send_message API 요청 성공')
    else:
        logger.error(f'send_message API 요청 실패: {response.text}')
        return False

    if item_data["changelog_show"] and changelog_html_path and not s3:
        api_url = f'{discord_api_url}/send_changelog'
        data = {'file': changelog_html_path, 'channel_id': item_data["discord_channel_id"]}
        try:
            response = requests.post(api_url, json=data, timeout=30)
        except requests.RequestException as e:
            logger.error(f'send_changelog API 요청 실패: {e}')
            return False
        if response.status_code == 200:
            logger.info('send_changelog API 요청 성공')
        else:
            logger.error(f'send_changelog API 요청 실패: {response.text}')
            return False

    return True

def update_version_file(version_file_path, version_json, item_name_list, download_short_list, fbx_only=False, new_fbx_records=None):
    """Cleans up and saves the updated version file."""
    if DRY_RUN:
        logger.info(f'Dry run: Skipping version file update for {version_file_path}.')
        return
        
    if not fbx_only:
        cleanup_version_json(version_json['files'])
    else:
        version_json['files'] = {}

    version_json['name-list'] = item_name_list
    version_json['short-list'] = download_short_list
    if new_fbx_records is not None:
        version_json['fbx-files'] = new_fbx_records
    elif 'fbx-files' not in version_json:
        version_json['fbx-files'] = {}
    
    # Atomic write: dump to a temp file in the same directory, fsync, then
    # os.replace so a crash mid-write cannot truncate the version file (which
    # would otherwise wipe short-list and trigger a full false re-notification).
    tmp_path = f'{version_file_path}.tmp'
    with open(tmp_path, 'w') as f:
        simdjson.dump(version_json, fp=f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, version_file_path)

def init_update_check(item): # This is the main orchestrator function
    item_data = prepare_item_data(item)
    order_num = item_data["order_num"]

    try:
        download_url_list, product_info_list, download_short_list, thumblist = fetch_booth_data(item_data)
    except BoothCrawlError as e:
        logger.debug(f"Crawling failed: {e}")
        return
    except Exception as e:
        logger.exception("An unexpected error occurred during fetch_booth_data")
        return

    # Crawl succeeded → clear any prior error state for this user so a future
    # failure will notify them again.
    reset_error_notification(item_data["discord_user_id"])

    product_name, product_url = product_info_list[0]
    if item_data["name"] is None:
        item_data["name"] = product_name

    version_file_path, version_json, download_list_changed = load_and_compare_version(order_num, download_short_list, item_data["fbx_only"])
    if version_file_path is None and version_json is None and not download_list_changed:
        return

    local_list = version_json.get('short-list', [])
    local_list_name = version_json.get('name-list', [])

    # Per-item working directories so concurrent worker threads never share
    # ./download/<name> or ./process/<path>. Cleaned up in finally.
    work_dir = tempfile.mkdtemp(prefix=f'{order_num}_', dir=WORK_ROOT)
    download_dir = os.path.join(work_dir, 'download')
    process_dir = os.path.join(work_dir, 'process')
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(process_dir, exist_ok=True)

    changelog_html_path = None
    try:
        item_name_list = process_files_for_changelog(item_data, download_url_list, local_list, download_dir)

        s3_object_url, summary_result = None, None
        diff_found = download_list_changed
        new_fbx_records = None

        if item_data["changelog_show"] or item_data["fbx_only"]:
            try:
                changelog_html_path, s3_object_url, summary_result, calc_diff_found, new_fbx_records = generate_changelog_and_summary(
                    item_data, download_url_list, version_json, download_dir, process_dir
                )
            except ChangelogError as e:
                logger.error(
                    f'변경점 분석 실패로 알림/버전 갱신을 건너뜁니다. '
                    f'다음 사이클에 재시도합니다. (order {order_num}): {e}'
                )
                return
            if item_data["fbx_only"]:
                diff_found = calc_diff_found
            elif item_data["changelog_show"]:
                diff_found = calc_diff_found or diff_found

        if item_data["fbx_only"] and not diff_found:
            logger.info(
                f'FBX 변경점이 없어 알림을 건너뛰고 버전 파일만 {download_short_list}(으)로 갱신합니다. (fbx_only)'
            )
            update_version_file(version_file_path, version_json, item_name_list, download_short_list, item_data["fbx_only"], new_fbx_records)
            return

        thumb = thumblist[0] if thumblist else "https://asset.booth.pm/assets/thumbnail_placeholder_f_150x150-73e650fbec3b150090cbda36377f1a3402c01e36fa067d01.png"

        delivered = send_discord_notification(
            item_data, (product_name, product_url), thumb, local_list_name,
            item_name_list, changelog_html_path, s3_object_url, summary_result
        )

        if not delivered:
            logger.error(
                f'Discord 전달이 확인되지 않아 버전 파일을 갱신하지 않습니다. '
                f'다음 사이클에 재발송됩니다. (order {order_num})'
            )
            return

        update_version_file(version_file_path, version_json, item_name_list, download_short_list, item_data["fbx_only"], new_fbx_records)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        # Drop the local changelog HTML once it has been delivered/uploaded.
        if changelog_html_path and not DRY_RUN and os.path.exists(changelog_html_path):
            try:
                os.remove(changelog_html_path)
            except OSError:
                pass

def generate_path_info(root, saved_prehash):
    path_list = []
    _generate_path_info_recursive(root, saved_prehash, path_list)
    return path_list

def _generate_path_info_recursive(root, saved_prehash, path_list, current_level=0):
    files = root.get('files')
    if not files:
        return

    for file_name, file_node in files.items():
        file_info = {'line_str': '', 'status': file_node['mark_as']}

        # 4 spaces per level: build_tree() derives depth via indent // 4, and
        # the FBX path (build_fbx_path_list) also uses 4-space indentation.
        file_info['line_str'] += ' ' * 4 * current_level

        symbol = ''
        if file_info['status'] == 1:
            symbol = '(Added)'
        elif file_info['status'] == 2:
            symbol = '(Deleted)'
        elif file_info['status'] == 3:
            symbol = '(Changed)'

        hash_val = file_node['hash']
        old_name = saved_prehash.get(hash_val)

        if old_name is not None:
            if file_info['status'] == 2:
                continue
            elif file_name != old_name:
                file_info['status'] = 0
                file_info['line_str'] += f'{old_name} → {file_name}'
            else:
                file_info['line_str'] += f'{file_name} {symbol}'
        else:
            file_info['line_str'] += f'{file_name} {symbol}'

        path_list.append(file_info)
        _generate_path_info_recursive(file_node, saved_prehash, path_list, current_level + 1)

def process_file_tree(input_path, filename, version_json, encoding, current_path, process_dir, fbx_only=False, fbx_records=None):
    current_path.append(filename)

    pathstr = '/'.join(current_path)

    isdir = os.path.isdir(input_path)
    filehash = ""
    if not isdir:
        filehash = calc_file_hash(input_path)
    else:
        filehash = "DIRECTORY"

    process_path = os.path.join(process_dir, pathstr)
    try:
        zip_type = try_extract(input_path, filename, process_path, encoding, process_dir)
    except Exception as e:
        # Propagate: a swallowed extraction failure would leave existing files
        # marked Deleted (element_mark pre-marks everything as deleted), so the
        # caller must abort the changelog and retry next cycle instead.
        logger.error(f'error occured on extracting {filename}: {e}')
        logger.debug(traceback.format_exc())
        current_path.pop()
        raise
    
    if not fbx_only:
        node = version_json
        for part in current_path[:-1]:
            node = node.setdefault('files', {}).setdefault(part, {})
        parent_dict = node.setdefault('files', {})
        file_node = parent_dict.get(filename)

        if file_node is None:
            parent_dict[filename] = {'hash': filehash, 'mark_as': 1}
        else:
            if file_node['hash'] == filehash:
                file_node['mark_as'] = 0
            else:
                file_node['hash'] = filehash
                file_node['mark_as'] = 3
    else:
        if not isdir and filename.lower().endswith('.fbx'):
            if fbx_records is not None:
                fbx_records[pathstr] = filehash
        
    if zip_type > 0 or os.path.isdir(process_path):
        for new_filename in os.listdir(process_path):
            new_process_path = os.path.join(process_path, new_filename)
            process_file_tree(new_process_path, new_filename, version_json, encoding, current_path, process_dir, fbx_only=fbx_only, fbx_records=fbx_records)

    current_path.pop()
    end_file_process(zip_type, process_path)
    
        
def end_file_process(zip_type, process_path):
    if zip_type > 0:
        shutil.rmtree(process_path)
    elif os.path.isdir(process_path):
        os.rmdir(process_path)
    else:
        os.remove(process_path)
    
# NOTE: Currently, @encoding only applies on zip_type == 1
def try_extract(input_path, input_filename, output_path, encoding, temp_dir):
    """Extracts a file if it's a zip or unitypackage, otherwise just moves it."""
    zip_type = is_compressed(input_path)

    if zip_type == 0:
        shutil.move(input_path, output_path)
        return zip_type

    # For compressed files, move to a temporary location for extraction.
    # temp_dir is the per-item working directory, so concurrent items cannot
    # collide on a shared CWD-root temp file.
    temp_output = os.path.join(temp_dir, f'__extract_tmp_{input_filename}')
    shutil.move(input_path, temp_output)
    os.makedirs(output_path, exist_ok=True)

    try:
        if zip_type == 1:  # zip
            with zipfile.ZipFile(temp_output, 'r', metadata_encoding=encoding) as zip_file:
                zip_file.extractall(output_path)
        elif zip_type == 2:  # unitypackage
            extractPackage(temp_output, outputPath=output_path)
    finally:
        os.remove(temp_output)

    return zip_type


def is_compressed(path):
###
# return
#   - 0: normal
#   - 1: zip
#   - 2: unitypackage
###
    if path.endswith('.zip'):
        return 1
    elif path.endswith('.unitypackage'):
        return 2
    
    return 0

def calc_file_hash(path):
    hash = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            hash.update(chunk)
    return hash.hexdigest()


def element_mark(root, mark_as, current_filename, prehash_dict): 
    root['mark_as'] = mark_as

    hash = root['hash']
    if (prehash_dict is not None and hash is not None
        and hash != 'DIRECTORY'):
        prehash_dict[hash] = current_filename

    files = root.get('files')
    if not files:
        return
        
    for file in files.keys():
        element_mark(root['files'][file], mark_as, file, prehash_dict)

def cleanup_version_json(files_root):
    """Recursively removes 'mark_as' and deletes nodes marked for deletion."""
    keys_to_delete = []
    for key, node in files_root.items():
        if node.get('mark_as') == 2:
            keys_to_delete.append(key)
            continue
        
        if 'mark_as' in node:
            del node['mark_as']
        
        if 'files' in node and node['files']:
            cleanup_version_json(node['files'])
            
    for key in keys_to_delete:
        del files_root[key]

def build_tree(paths):
    tree = {}
    path_stack = []
    for item in paths:
        line_str = item.get('line_str', '')
        status = item.get('status', 0)

        # 후행 공백만 제거하여 선행 공백을 보존
        line_str = line_str.rstrip()

        # 선행 공백의 수를 계산하여 들여쓰기 수준 결정
        indent_match = re.match(r'^(\s*)(.*)', line_str)
        if indent_match:
            leading_spaces = indent_match.group(1)
            indent = len(leading_spaces)
            content = indent_match.group(2)
        else:
            indent = 0
            content = line_str

        # content에서 상태 문자열 제거
        content = re.sub(r'\s*\(.*\)$', '', content)

        # 깊이 계산 (들여쓰기 수준에 따라)
        depth = indent // 4  # 공백 4칸당 한 레벨로 설정 (필요에 따라 조정)

        # 현재 깊이에 맞게 경로 스택 조정
        path_stack = path_stack[:depth]
        path_stack.append(content)

        # 트리 빌드
        node = tree
        for part in path_stack[:-1]:
            node = node.setdefault(part, {})
        # 현재 노드에 상태 정보 저장
        current_node = node.setdefault(path_stack[-1], {})
        current_node['_status'] = status
    return tree

def tree_to_html(tree):
    html = '<ul>\n'  # 시작 태그에 줄바꿈 추가
    for key, subtree in tree.items():
        if key == '_status':
            continue  # 상태 정보는 별도로 처리
        status = subtree.get('_status', 0)

        # 상태에 따른 컬러 지정
        line_color = 'rgb(255, 255, 255)'  # 기본 색상 (흰색)
        if status == 1:
            line_color = 'rgb(125, 164, 68)'  # Added (녹색 계열)
        elif status == 2:
            line_color = 'rgb(252, 101, 89)'  # Deleted (빨간색 계열)
        elif status == 3:
            line_color = 'rgb(128, 161, 209)'  # Changed (파란색 계열)

        # 상태 문자열 추가
        status_str = ''
        if status == 1:
            status_str = ' (Added)'
        elif status == 2:
            status_str = ' (Deleted)'
        elif status == 3:
            status_str = ' (Changed)'

        # '_status' 키를 제외한 나머지로 재귀 호출
        child_subtree = {k: v for k, v in subtree.items() if k != '_status'}

        if child_subtree:
            # 자식이 있는 경우
            html += f'<li><span style="color:{line_color}">{key}{status_str}</span>\n'
            html += tree_to_html(child_subtree)
            html += '</li>\n'
        else:
            # 자식이 없는 경우
            html += f'<li><span style="color:{line_color}">{key}{status_str}</span></li>\n'
    html += '</ul>\n'  # 마지막 태그에 줄바꿈 추가
    return html

def files_list(tree):
    raw_data = ''

    for key, subtree in tree.items():
        if key == '_status':
            continue  # 상태 정보는 별도로 처리
        status = subtree.get('_status', 0)
        # 자식 노드들 처리
        child_subtree = {k: v for k, v in subtree.items() if k != '_status'}
        child_data = files_list(child_subtree) if child_subtree else ''

        if status != 0:
            # 상태 문자열 결정
            if status == 1:
                status_str = ' (Added)'
            elif status == 2:
                status_str = ' (Deleted)'
            elif status == 3:
                status_str = ' (Changed)'
            else:
                status_str = ''

            raw_data += f'{key}{status_str}\n'
            raw_data += child_data
        else:
            # 현재 노드의 status가 0이면 출력 안 하고 자식 노드들만 처리
            raw_data += child_data

    return raw_data

def send_error_message(discord_channel_id, discord_user_id):
    if DRY_RUN:
        logger.info('Dry run: Skipping Discord error notification.')
        return

    api_url = f'{discord_api_url}/send_error_message'

    data = {
        'channel_id': discord_channel_id,
        'user_id': discord_user_id
    }

    try:
        response = requests.post(api_url, json=data, timeout=30)
    except requests.RequestException as e:
        logger.error(f'send_error_message API 요청 실패: {e}')
        return

    if response.status_code == 200:
        logger.info('send_error_message API 요청 성공')
    else:
        logger.error(f'send_error_message API 요청 실패: {response.text}')
    return

def reset_error_notification(discord_user_id):
    """Best-effort: tell the discord service to clear a user's accumulated error
    state after a successful crawl, so a later failure re-notifies them. Deduped
    per cycle to avoid one request per item."""
    if DRY_RUN or discord_user_id is None:
        return
    with reset_users_lock:
        if discord_user_id in reset_users_this_cycle:
            return
        reset_users_this_cycle.add(discord_user_id)
    try:
        requests.post(f'{discord_api_url}/reset_error', json={'user_id': discord_user_id}, timeout=10)
    except requests.RequestException as e:
        logger.debug(f'reset_error 요청 실패(무시): {e}')

def recreate_folder(path):
    """Deletes a folder and all its contents, then recreates it."""
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)

def prune_old_archives(base_path, retention_days):
    """Removes archive timestamp folders older than retention_days.

    Archives live at ./archive/<order_num>/<timestamp>/. retention_days <= 0
    disables pruning (keep forever).
    """
    if retention_days <= 0 or not os.path.isdir(base_path):
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for order_dir in os.listdir(base_path):
        order_path = os.path.join(base_path, order_dir)
        if not os.path.isdir(order_path):
            continue
        for ts_dir in os.listdir(order_path):
            ts_path = os.path.join(order_path, ts_dir)
            if not os.path.isdir(ts_path):
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(ts_path))
            except OSError:
                continue
            if mtime < cutoff:
                logger.info(f'Pruning old archive: {ts_path}')
                shutil.rmtree(ts_path, ignore_errors=True)
        # Drop the order folder if it is now empty.
        try:
            if not os.listdir(order_path):
                os.rmdir(order_path)
        except OSError:
            pass

def run_update_check_safely(item):
    thread_local.order_num = item[0]
    try:
        init_update_check(item)
    except PermissionError:
        logger.error('PermissionError occured')
    except Exception as e:
        logger.exception('An unexpected error occurred while checking item.')
    finally:
        if hasattr(thread_local, 'order_num'):
            del thread_local.order_num

def strftime_now():
    return datetime.now().strftime('%Y%m%d-%H%M%S')

if __name__ == "__main__":
    with open("config.json") as file:
        config_json = simdjson.load(file)

    logging_config = config_json.get('logging', {})
    syslog_config = logging_config.get('syslog', {})
    attach_syslog_handler(logger, syslog_config, formatter)
    if syslog_config.get('enabled') and syslog_config.get('address'):
        port_value = syslog_config.get('port', 514)
        try:
            port = int(port_value)
        except (TypeError, ValueError):
            port = port_value
        logger.info("Syslog logging enabled: sending logs to %s:%s", syslog_config.get('address'), port)
        
    # Configure global settings
    discord_api_url = config_json['discord_api_url']
    gemini_api_key = config_json.get('gemini_api_key')
    if gemini_api_key:
        summary = llm_summary.google_gemini_api(gemini_api_key)
    else:
        summary = None
        logger.info("Gemini API key not found")
        
    refresh_interval = int(config_json['refresh_interval'])

    # Archive retention in days (<= 0 keeps forever). Pruned at each cycle start.
    archive_retention_days = int(config_json.get('archive_retention_days', 30))

    DRY_RUN = config_json.get('dry_run', False)
    logger.info(f"Dry run is {'enabled' if DRY_RUN else 'disabled'}.")

    # Calculate default workers based on CPU count
    # https://booth.pm/announcements/863 공지로 인한 수정
    # cpu_cores = os.cpu_count()
    # default_workers = (cpu_cores + 4) if cpu_cores is not None else 8
    default_workers = 2
    max_workers = int(config_json.get('max_workers', default_workers))
    logger.info(f"Using {max_workers} worker threads for parallel processing.")
    
    s3_uploader = None
    s3 = config_json.get('s3')
    if s3:
        try:
            s3_uploader = cloudflare.S3Uploader(s3['endpoint_url'], s3['access_key_id'], s3['secret_access_key'])
        except Exception as e:
            logger.error(f"Failed to initialize S3 uploader: {e}")
    else:
        s3 = None

    createFolder("./version")
    createFolder("./version/db")
    createFolder("./version/json")
    createFolder("./archive")
    createFolder("./changelog")
    createFolder(WORK_ROOT)

    postgres_config = dict(config_json['postgres'])
    booth_db = booth_sql.BoothPostgres(postgres_config)

    if not DRY_RUN:
        # booth_discord 컨테이너 시작 대기
        logger.info("Waiting for booth_discord container to start...")
        # A simple heartbeat check for the discord API
        for _ in range(5): # Try 5 times
            try:
                response = requests.get(f"{discord_api_url}/", timeout=5)
                if response.status_code == 404: # Quart returns 404 for base URL by default
                    logger.info("booth_discord container is ready.")
                    break
            except requests.ConnectionError:
                logger.info("booth_discord not ready yet, waiting...")
                sleep(5)
        else:
            logger.error("Could not connect to booth_discord container. Exiting.")
            exit(1)
    else:
        logger.info("Dry run enabled, skipping booth_discord container check.")

    while True:
        logger.info("BoothChecker cycle started")

        # Recreate the working root and prune stale archives.
        recreate_folder(WORK_ROOT)
        prune_old_archives("./archive", archive_retention_days)
        with reset_users_lock:
            reset_users_this_cycle.clear()

        booth_items = booth_db.get_booth_items()
        logger.info(f"Found {len(booth_items)} items to check.")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.map(run_update_check_safely, booth_items)
            
        # 갱신 대기
        logger.info("BoothChecker cycle finished")
        logger.info(f"Next check will be at {datetime.now() + timedelta(seconds=refresh_interval)}")
        sleep(refresh_interval)
