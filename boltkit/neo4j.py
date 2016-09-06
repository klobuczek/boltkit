#!/usr/bin/env python
# coding: utf-8

# Copyright (c) 2002-2016 "Neo Technology,"
# Network Engine for Objects in Lund AB [http://neotechnology.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from argparse import ArgumentParser, RawDescriptionHelpFormatter
from base64 import b64encode
from hashlib import sha256
from json import loads as json_loads
from os import getenv, makedirs
from os.path import join as path_join, normpath
import platform
from random import randint
from socket import create_connection
from subprocess import check_output
from sys import stderr, stdin
from time import sleep
try:
    from urllib.request import urlopen, Request, HTTPError
    from urllib.parse import urlparse
except ImportError:
    from urllib2 import urlopen, Request, HTTPError
    from urlparse import urlparse


DIST_HOST = "dist.neo4j.org"


class Downloader(object):

    def __init__(self, path, verbose=False):
        self.path = path
        self.verbose = verbose

    def write(self, message):
        if self.verbose:
            stderr.write(message)

    def download_build(self, address, build_id, package):
        """ Download from TeamCity build server.
        """
        url = "https://%s/repository/download/%s/.lastSuccessful/%s" % (address, build_id, package)
        user = getenv("TEAMCITY_USER")
        password = getenv("TEAMCITY_PASSWORD")
        auth_token = b"Basic " + b64encode(("%s:%s" % (user, password)).encode("iso-8859-1"))
        request = Request(url, headers={"Authorization": auth_token})
        package_path = path_join(self.path, package)

        self.write("Downloading build from %s... " % url)
        fin = urlopen(request)
        try:
            with open(package_path, "wb") as fout:
                more = True
                while more:
                    data = fin.read(8192)
                    if data:
                        fout.write(data)
                    else:
                        more = False
        finally:
            fin.close()

        return package_path

    def download_dist(self, address, package):
        """ Download from public distributions.
        """
        url = "http://%s/%s" % (address, package)
        package_path = path_join(self.path, package)

        self.write("Downloading dist from %s... " % url)
        fin = urlopen(url)
        try:
            with open(package_path, "wb") as fout:
                more = True
                while more:
                    data = fin.read(8192)
                    if data:
                        fout.write(data)
                    else:
                        more = False
        finally:
            fin.close()

        return package_path

    def download(self, edition, version, package_format):
        version_parts = version.replace("-", ".").replace("_", ".").split(".")
        if len(version_parts) == 3:
            major, minor, patch = version_parts
            milestone = ""
        elif len(version_parts) == 4:
            major, minor, patch, milestone = version_parts
        else:
            raise ValueError("Unrecognised version %r" % version)

        # Derive template and package version
        if package_format in ("unix.tar.gz", "windows.zip"):
            template = "neo4j-%s-%s-%s"
            if milestone:
                package_version = "%s.%s.%s-%s" % (major, minor, patch, milestone)
            else:
                package_version = "%s.%s.%s" % (major, minor, patch)
        else:
            raise ValueError("Unknown format %r" % package_format)
        package = template % (edition, package_version, package_format)

        dist_address = getenv("DIST_HOST", DIST_HOST)
        build_address = getenv("TEAMCITY_HOST")
        if build_address:
            try:
                build_id = "Neo4j%s%s_Packaging" % (major, minor)
                package = self.download_build(build_address, build_id, package)
            except HTTPError:
                package = self.download_dist(dist_address, package)
        else:
            package = self.download_dist(dist_address, package)
        self.write("\r\n")

        return package


def untar(archive, path):
    from tarfile import TarFile
    with TarFile.open(archive) as files:
        files.extractall(path)
        return path_join(path, files.getnames()[0])


def unzip(archive, path):
    from zipfile import ZipFile
    with ZipFile(archive, 'r') as files:
        files.extractall(path)
        return path_join(path, files.namelist()[0])


def wait_for_server(host, port, timeout=30):
    running = False
    t = 0
    while not running and t < timeout:
        try:
            s = create_connection((host, port or 7474))
        except IOError:
            sleep(1)
            t += 1
        else:
            s.close()
            running = True


def hex_bytes(data):
    return b"".join(b"%02X" % b for b in bytearray(data))


def user_record(user, password):
    salt = bytearray(randint(0x00, 0xFF) for _ in range(16))
    m = sha256()
    m.update(salt)
    m.update(password)
    return b"%s:SHA-256,%s,%s:" % (user, hex_bytes(m.digest()), hex_bytes(salt))


class Unix(object):

    @classmethod
    def download(cls, edition, version, path, **kwargs):
        downloader = Downloader(normpath(path), verbose=kwargs.get("verbose"))
        return downloader.download(edition, version, "unix.tar.gz")

    @classmethod
    def install(cls, edition, version, path, **kwargs):
        package = cls.download(edition, version, path, **kwargs)
        home = untar(package, path)
        return home

    @classmethod
    def start(cls, home, **kwargs):
        out = check_output([path_join(home, "bin", "neo4j"), "start"])
        uri = None
        parsed_uri = None
        for line in out.splitlines():
            http = line.find(b"http:")
            if http >= 0:
                uri = line[http:].split()[0].strip()
                parsed_uri = urlparse(uri)
        if not parsed_uri:
            raise RuntimeError("Cannot ascertain server address")
        wait_for_server(parsed_uri.hostname, parsed_uri.port)
        return uri.decode("iso-8859-1")

    @classmethod
    def stop(cls, home, **kwargs):
        check_output([path_join(home, "bin", "neo4j"), "stop"])

    @classmethod
    def create_user(cls, home, user, password, **kwargs):
        data_dbms = path_join(home, "data", "dbms")
        try:
            makedirs(data_dbms)
        except OSError:
            pass
        with open(path_join(data_dbms, "auth"), "a") as f:
            f.write(user_record(user, password))
            f.write(b"\r\n")


class Windows(object):

    @classmethod
    def download(cls, edition, version, path, **kwargs):
        downloader = Downloader(normpath(path), verbose=kwargs.get("verbose"))
        return downloader.download(edition, version, "windows.zip")

    @classmethod
    def install(cls, edition, version, path, **kwargs):
        package = cls.download(edition, version, path, **kwargs)
        home = unzip(package, path)
        return home

    @classmethod
    def start(cls, home, **kwargs):
        raise NotImplementedError("Windows support not complete")

    @classmethod
    def stop(cls, home, verbose=False):
        raise NotImplementedError("Windows support not complete")

    @classmethod
    def create_user(cls, home, user, password, **kwargs):
        raise NotImplementedError("Windows support not complete")


def usage():
    print("usage: neotest-download [-h] [-e] [-v] version [path]")
    print("       neotest-install [-h] [-e] [-v] version [path]")
    print("       neotest-start [-h] [-v] home")
    print("       neotest-stop [-h] [-v] home")
    print("       neotest-create-user [-h] [-v] home user password")


def download():
    parser = ArgumentParser(
        description="Download a Neo4j server package for the current platform.\r\n"
                    "\r\n"
                    "example:\r\n"
                    "  neotest-download -e 3.1.0-M09 $HOME/servers/",
        epilog="environment variables:\r\n"
               "  DIST_HOST         name of distribution server (default: %s)\r\n"
               "  TEAMCITY_HOST     name of build server (optional)\r\n"
               "  TEAMCITY_USER     build server user name (optional)\r\n"
               "  TEAMCITY_PASSWORD build server password (optional)\r\n"
               "\r\n"
               "If TEAMCITY_* environment variables are set, the build server will be checked\r\n"
               "for the package before the distribution server. Note that supplying a local\r\n"
               "alternative DIST_HOST can help reduce test timings on a slow network.\r\n"
               "\r\n"
               "Report bugs to drivers@neo4j.com" % (DIST_HOST,),
        formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-e", "--enterprise", action="store_true",
                        help="select Neo4j Enterprise Edition (default: Community)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show more detailed output")
    parser.add_argument("version", help="Neo4j server version")
    parser.add_argument("path", nargs="?", default=".",
                        help="download destination path (default: .)")
    parsed = parser.parse_args()
    edition = "enterprise" if parsed.enterprise else "community"
    try:
        if platform.system() == "Windows":
            home = Windows.download(edition, parsed.version, parsed.path, verbose=parsed.verbose)
        else:
            home = Unix.download(edition, parsed.version, parsed.path, verbose=parsed.verbose)
    except HTTPError as error:
        if error.code == 401:
            stderr.write("ERROR: Missing or incorrect authorization\r\n")
            exit(1)
        elif error.code == 403:
            stderr.write("ERROR: Could not download package from %s "
                         "(403 Forbidden)\r\n" % error.url)
            exit(1)
        else:
            raise
    else:
        print(home)


def install():
    parser = ArgumentParser(
        description="Download and extract a Neo4j server package for the current platform.\r\n"
                    "\r\n"
                    "example:\r\n"
                    "  neotest-install -e 3.1.0-M09 $HOME/servers/",
        epilog="See neotest-download for details of supported environment variables.\r\n"
               "\r\n"
               "Report bugs to drivers@neo4j.com",
        formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-e", "--enterprise", action="store_true",
                        help="select Neo4j Enterprise Edition (default: Community)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show more detailed output")
    parser.add_argument("version", help="Neo4j server version")
    parser.add_argument("path", nargs="?", default=".",
                        help="download destination path (default: .)")
    parsed = parser.parse_args()
    edition = "enterprise" if parsed.enterprise else "community"
    try:
        if platform.system() == "Windows":
            home = Windows.install(edition, parsed.version, parsed.path, verbose=parsed.verbose)
        else:
            home = Unix.install(edition, parsed.version, parsed.path, verbose=parsed.verbose)
    except HTTPError as error:
        if error.code == 401:
            stderr.write("ERROR: Missing or incorrect authorization\r\n")
            exit(1)
        elif error.code == 403:
            stderr.write("ERROR: Could not download package from %s "
                         "(403 Forbidden)\r\n" % error.url)
            exit(1)
        else:
            raise
    else:
        print(home)


def start():
    parser = ArgumentParser(
        description="Start an installed Neo4j server instance.\r\n"
                    "\r\n"
                    "example:\r\n"
                    "  neotest-start $HOME/servers/neo4j-community-3.0.0",
        epilog="Report bugs to drivers@neo4j.com",
        formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show more detailed output")
    parser.add_argument("home", help="Neo4j server directory")
    parsed = parser.parse_args()
    if platform.system() == "Windows":
        http_uri = Windows.start(parsed.home, verbose=parsed.verbose)
    else:
        http_uri = Unix.start(parsed.home, verbose=parsed.verbose)
    if http_uri:
        f = urlopen(http_uri)
        try:
            data = f.read()
        finally:
            f.close()
        uris = json_loads(data.decode("utf-8"))
        bolt_uri = uris["bolt"]
        print("%s %s" % (http_uri, bolt_uri))


def stop():
    parser = ArgumentParser(
        description="Stop an installed Neo4j server instance.\r\n"
                    "\r\n"
                    "example:\r\n"
                    "  neotest-stop $HOME/servers/neo4j-community-3.0.0",
        epilog="Report bugs to drivers@neo4j.com",
        formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show more detailed output")
    parser.add_argument("home", help="Neo4j server directory")
    parsed = parser.parse_args()
    if platform.system() == "Windows":
        Windows.stop(parsed.home, verbose=parsed.verbose)
    else:
        Unix.stop(parsed.home, verbose=parsed.verbose)


def create_user():
    parser = ArgumentParser(
        description="Create a new Neo4j user.\r\n"
                    "\r\n"
                    "example:\r\n"
                    "  neotest-create-user $HOME/servers/neo4j-community-3.0.0 bob s3cr3t",
        epilog="Report bugs to drivers@neo4j.com",
        formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show more detailed output")
    parser.add_argument("home", help="Neo4j server directory")
    parser.add_argument("user", help="name of new user")
    parser.add_argument("password", help="password for new user")
    parsed = parser.parse_args()
    user = parsed.user.encode(stdin.encoding or "utf-8")
    password = parsed.password.encode(stdin.encoding or "utf-8")
    if platform.system() == "Windows":
        Windows.create_user(parsed.home, user, password, verbose=parsed.verbose)
    else:
        Unix.create_user(parsed.home, user, password, verbose=parsed.verbose)