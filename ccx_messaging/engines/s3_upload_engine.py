# Copyright 2024 Red Hat Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""S3 Engine Class and related functions."""

import json
import re
import logging
from collections import ChainMap

from insights_messaging.engine import Engine

from ccx_messaging.utils.s3_uploader import S3Uploader
from ccx_messaging.utils.sliced_template import SlicedTemplate
from ccx_messaging.error import CCXMessagingError


# Path example: <org_id>/<cluster_id>/<year><month><day><time>-<id>
# Following RE matches with S3 archives like the previous example and allow
# to extract named groups for the different meaningful components
S3_ARCHIVE_PATTERN = re.compile(
    r"(?P<org_id>[0-9]+)\/"  # extract named group for organization id
    r"(?P<cluster_id>[0-9,a-z,-]{36})\/"  # extract named group for the cluster_id
    r"(?P<archive>"  # extract named group for the archive name, including the following 3 lines
    r"(?P<timestamp>"  # extract the timestamp named group, including the following line
    # Next line extract year, month, day and time named groups from the timestamp
    r"(?P<year>[0-9]{4})(?P<month>[0-9]{2})(?P<day>[0-9]{2})(?P<time>[0-9]{6}))-"
    r"(?P<id>[a-z,A-Z,0-9]*))"  # Extract the id of the file as named group
)
LOG = logging.getLogger(__name__)


def create_metadata(components: dict[str, str]):
    """Create a metadata for Publishing."""
    msg_metadata = {
        "cluster_id": components.get("cluster_id"),
        "external_organization": components.get("org_id"),
    }
    LOG.debug("msg_metadata %s", msg_metadata)
    return msg_metadata


class S3UploadEngine(Engine):
    """Engine for processing the metadata and ceph path of downloaded archive and uploading it to ceph bucket."""  # noqa: E501

    def __init__(
        self,
        formatter,
        target_components=None,
        extract_timeout=None,
        extract_tmp_dir=None,
        dest_bucket=None,
        access_key=None,
        secret_key=None,
        endpoint=None,
        archives_path_prefix=None,
        archive_name_pattern="$cluster_id[:2]/$cluster_id/$year$month/$day/$time.tar.gz",
    ):
        """Initialize engine for S3 upload.

        The S3 server is specified by the `endpoint` argument, and its credentials using
        `access_key` and `secret_key` arguments.

        The bucket where the archives will be uploaded is specified by `dest_bucket` argument.

        A common prefix for all the archives uploaded can be defined using `archives_path_prefix`,
        that should be a `str` without the starting /.

        `archive_name_pattern` will define a string template. The following substitutions are
        available:
          - `cluster_id`: it will be replaced by the cluster ID for the processed archive.
          - `timestamp`, `year`, `month`, `day` and `time`: those will be extracted from incoming
            archive path or other availables timestamps.
          - `archive`: it will be replaced by the base name of the archive.
        """
        super().__init__(formatter, target_components, extract_timeout, extract_tmp_dir)
        self.dest_bucket = dest_bucket
        self.archives_path_prefix = archives_path_prefix
        self.archive_name_template = SlicedTemplate(archive_name_pattern)
        self.uploader = S3Uploader(
            access_key=access_key,
            secret_key=secret_key,
            endpoint=endpoint,
        )

    def process(self, broker, local_path):
        """Create metadata and target_path from downloaded archive and uploads it to ceph bucket."""
        LOG.info("Processing %s for uploading", local_path)
        self.fire("pre_extract", broker, local_path)

        s3_path = broker["s3_path"]

        for w in self.watchers:
            w.watch_broker(broker)

        match_ = S3_ARCHIVE_PATTERN.match(s3_path)
        if not match_:
            LOG.warning("The archive doesn't match the expected pattern: %s")
            exception = CCXMessagingError("Archive pattern name incorrect")
            self.fire("on_engine_failure", broker, exception)
            raise exception

        components = ChainMap(broker, match_.groupdict())
        target_path = self.compute_target_path(components)
        LOG.info(f"Uploading archive '{s3_path}' as {self.dest_bucket}/{target_path}")
        self.uploader.upload_file(local_path, self.dest_bucket, target_path)
        LOG.info(f"Uploaded archive '{s3_path}' as {self.dest_bucket}/{target_path}")

        metadata = create_metadata(components)
        report = {
            "path": target_path,
            "original_path": s3_path,
            "metadata": metadata,
        }

        LOG.info("Generated report: %s", report)
        self.fire("on_engine_success", broker, report)

        del broker["cluster_id"]
        del broker["s3_path"]
        return json.dumps(report)

    def compute_target_path(self, components: dict[str, str]) -> str:
        """Compute S3 target path from the found archive name components.

        Target path is defined by the archive pattern.
        """
        path = self.archive_name_template.safe_substitute(components)

        if self.archives_path_prefix:
            return f"{self.archives_path_prefix}/{path}"

        else:
            return path
