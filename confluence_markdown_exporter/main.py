from atlassian import Confluence
from atlassian.errors import ApiError
from dotenv import load_dotenv
from markdownify import MarkdownConverter
from typing import TypedDict, List, Set
from urllib.parse import urlparse, urlunparse

import bs4
import configargparse
import json
import logging
import os
import requests
import sys


ATTACHMENT_FOLDER_NAME = "attachments"
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024   # 4MB, since we're single threaded this is safe to raise much higher


class PageDescription(TypedDict):
    sanitized_filename: str
    sanitized_parents: List[str]
    document_name: str
    page_location: List[str]
    page_filename: str
    page_output_dir: str


class ExportException(Exception):
    pass

def suppress_confluence_info_logs():
    # Confluence module likes to log url endpoint for every request.
    # dumb workaround to suppress INFO logs
    confluence_logger = logging.getLogger('atlassian.confluence')
    confluence_logger.setLevel(logging.WARNING)


class Exporter:
    def __init__(self, url, username, token, out_dir, space: str, no_attach):
        self.__out_dir = out_dir
        self.__parsed_url = urlparse(url)
        self.__username = username
        self.__token = token
        suppress_confluence_info_logs()
        self.__confluence = Confluence(url=urlunparse(self.__parsed_url),
                                       username=self.__username,
                                       password=self.__token)
        self.__seen: Set[int] = set()
        self.__no_attach = no_attach
        self.__target_space_key = space if space.startswith("~") else "~" + space

    def __should_skip_download(self, page_id, last_modified):
        """Check if the page should be skipped based on the index."""

        # This functionality is not currently implemented

        return True

    def __sanitize_filename(self, document_name_raw) -> str:
        document_name = document_name_raw
        for invalid in ["..", "/"]:
            if invalid in document_name:
                logging.warning("Dangerous page title: \"{}\", \"{}\" found, replacing it with \"_\"".format(
                    document_name,
                    invalid))
                document_name = document_name.replace(invalid, "_")
        return document_name

    def __get_descr(self, page, parents, is_leaf_node) -> PageDescription:
        """
        Get a description of the given page with various properties.

        :param page: The page for which the description is needed.
        :return: A dictionary containing sanitized_filename, document_name, 
                 page_location, page_filename, and page_output_dir.
        """

        # save all files as .html for now, we will convert them later
        extension = ".html"
        if is_leaf_node:
            document_name = page["title"] + extension
        else:
            document_name = "index" + extension

        # make some rudimentary checks, to prevent trivial errors
        sanitized_filename = self.__sanitize_filename(document_name)
        sanitized_parents: List[str] = list(map(self.__sanitize_filename, parents))

        page_location: List[str] = sanitized_parents + [sanitized_filename]
        page_filename = os.path.join(self.__out_dir, *page_location)

        page_output_dir = os.path.dirname(page_filename)

        return {
            'sanitized_filename': sanitized_filename,
            'sanitized_parents': sanitized_parents,
            'document_name': document_name,
            'page_location': page_location,
            'page_filename': page_filename,
            'page_output_dir': page_output_dir
        }


    def __download_page(self, page, parents, is_leaf_node, when_modified):
        content = page["body"]["storage"]["value"]

        descr = self.__get_descr(page, parents, is_leaf_node)
        os.makedirs(descr['page_output_dir'], exist_ok=True)
        logging.info("Saving to {}".format(" / ".join(descr['page_location'])))

        with open(descr['page_filename'], "w", encoding="utf-8") as f:
            f.write(content)

        # fetch attachments unless disabled
        if not self.__no_attach:
            ret = self.__confluence.get_attachments_from_content(page["id"], start=0, limit=500, expand=None,
                                                                 filename=None, media_type=None)
            for i in ret["results"]:
                att_title = i["title"]
                download = i["_links"]["download"]

                att_url = urlunparse(
                    (self.__parsed_url[0], self.__parsed_url[1], "/wiki/" + download.lstrip("/"), None, None, None)
                )
                att_sanitized_name = self.__sanitize_filename(att_title)
                att_filename = os.path.join(descr['page_output_dir'], ATTACHMENT_FOLDER_NAME, att_sanitized_name)

                att_dirname = os.path.dirname(att_filename)
                os.makedirs(att_dirname, exist_ok=True)

                logging.debug("Saving attachment {} to {}".format(att_title, descr['page_location']))

                r = requests.get(att_url, auth=(self.__username, self.__token), stream=True)
                if 400 <= r.status_code:
                    if r.status_code == 404:
                        logging.warning("Attachment {} not found (404)!".format(att_url))
                        continue

                    # this is a real error, raise it
                    r.raise_for_status()

                with open(att_filename, "wb") as f:
                    for buf in r.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        f.write(buf)

        self.__seen.add(page["id"])
        self.__update_index(page['id'], when_modified)


    def __dump_page(self, src_id: int, parents) -> None:
        if src_id in self.__seen:
            # this could theoretically happen if Page IDs are not unique or there is a circle
            raise ExportException("Duplicate Page ID Found!")

        page = self.__confluence.get_page_by_id(src_id, expand="body.storage")
        page_title = page["title"]
        page_id = page["id"]

        properties = self.__confluence.get_page_properties(page_id)
        when_modified = properties["results"][0]["version"]['when']
    
        # see if there are any children
        child_ids = self.__confluence.get_child_id_list(page_id)
        is_leaf_node = len(child_ids) == 0

        page_descr = self.__get_descr(page, parents, is_leaf_node)

        if self.__should_skip_download(page_id, when_modified) == False:
            self.__download_page(page,  parents, is_leaf_node, when_modified)

    
        # recurse to process child nodes
        for child_id in child_ids:
            self.__dump_page(child_id, parents=page_descr['sanitized_parents'] + [page_title])

    def dump_target_space(self) -> None: 
        logging.debug(f"Looking for target space {self.__target_space_key}")
        try:
            ret = self.__confluence.get_space(self.__target_space_key)

        except ApiError as e:
            err_msg: str = e.reason.args[0]
            confluence_not_found_err_class = "com.atlassian.confluence.api.service.exceptions.NotFoundException"
            not_found = err_msg.startswith(confluence_not_found_err_class)

            if not_found:
                msg = err_msg[len(confluence_not_found_err_class)]
                logging.error(msg)
                return
            else:
                logging.error("Unknown error encountered while retrieving the target space: {e}")
                return

        if ret.get('homepage') is None:
            logging.error("Specified space was found, but no 'homepage' was marked.")
            return

        logging.info(f"Found target space {self.__target_space_key}, downloading it...")
        self.__dump_page(ret['homepage']['id'], parents=[ret['key']])


    def dump(self) -> None:
        if self.__target_space_key:
            self.dump_target_space()
        else:
            self.dump_all_spaces()

    def dump_all_spaces(self) -> None:
        ret = self.__confluence.get_all_spaces(start=0, limit=500, expand='description.plain,homepage')
        if ret['size'] == 0:
            logging.error("No spaces found in confluence. Please check credentials")
        for space in ret["results"]:
            space_key = space["key"]
            logging.debug("Processing space", space_key)
            if space.get("homepage") is None:
                logging.warning("Skipping space: {}, no homepage found!".format(space_key))
                logging.warning("In order for this tool to work there has to be a root page!")
                raise ExportException(f"No homepage found for space {space_key}")
            else:
                # homepage found, recurse from there
                homepage_id = space["homepage"]["id"]
                self.__dump_page(homepage_id, parents=[space_key])


class Converter:
    def __init__(self, out_dir):
        self.__out_dir = out_dir

    def recurse_findfiles(self, path):
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                yield from self.recurse_findfiles(entry.path)
            elif entry.is_file(follow_symlinks=False):
                yield entry
            else:
                raise NotImplemented()

    def __convert_atlassian_html(self, soup):
        for image in soup.find_all("ac:image"):
            url = None
            for child in image.children:
                url = child.get("ri:filename", None)
                break

            if url is None:
                # no url found for ac:image
                continue

            # construct new, actually valid HTML tag
            srcurl = os.path.join(ATTACHMENT_FOLDER_NAME, url)
            imgtag = soup.new_tag("img", attrs={"src": srcurl, "alt": srcurl})

            # insert a linebreak after the original "ac:image" tag, then replace with an actual img tag
            image.insert_after(soup.new_tag("br"))
            image.replace_with(imgtag)
        return soup

    def convert(self):
        for entry in self.recurse_findfiles(self.__out_dir):
            path = entry.path

            if not path.endswith(".html"):
                continue

            logging.info("Converting {}".format(path))
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()

            soup_raw = bs4.BeautifulSoup(data, 'html.parser')
            soup = self.__convert_atlassian_html(soup_raw)

            md = MarkdownConverter().convert_soup(soup)
            newname = os.path.splitext(path)[0]
            with open(newname + ".md", "w", encoding="utf-8") as f:
                f.write(md)

if __name__ == "__main__":
    load_dotenv()

    parser = configargparse.ArgumentParser()
    parser.add_argument("--url", type=str, required=True, help="The url to the confluence instance", env_var="PYCNFL_URL")
    parser.add_argument("--username", type=str, required=True, help="The username", env_var="PYCNFL_USERNAME")
    parser.add_argument("--token", type=str, required=True, help="The access token to Confluence", env_var="PYCNFL_TOKEN")
    parser.add_argument("--out_dir", type=str, required=True, help="The directory to output the files to", env_var="PYCNFL_OUT_DIR")
    parser.add_argument("--personal-space-key", type=str, required=False, default=None, help="Spaces to export", env_var="PYCNFL_PERSONAL_SPACE_KEY")
    parser.add_argument("--skip-attachments", action="store_true", dest="no_attach", required=False,
                        default=False, help="Skip fetching attachments", env_var="PYNCFL_SKIP_ATTACHMENTS")
    parser.add_argument("--no-fetch", action="store_true", dest="no_fetch", required=False,
                        default=False, help="This option only runs the markdown conversion", env_var="PYNCFL_NO_FETCH")
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,  # Set default logging level to INFO
        stream=sys.stdout,   # Set default output to stdout
        format='%(asctime)s [%(filename)s:%(lineno)d] %(levelname)s: %(message)s',  # Include ISO 8601 timestamp
        datefmt='%Y-%m-%dT%H:%M:%S'  # ISO 8601 date format
    )
    
    if not args.no_fetch:
        dumper = Exporter(url=args.url, username=args.username, token=args.token, out_dir=args.out_dir,
                          space=args.personal_space_key, no_attach=args.no_attach)
        dumper.dump()
    
    converter = Converter(out_dir=args.out_dir)
    converter.convert()
