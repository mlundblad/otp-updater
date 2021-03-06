#!/usr/bin/env python3
# -*- encoding: utf-8 -*-

# Copyright (c) 2015 Marcus Lundblad <ml@update.uu.se>
#
# otp-updater.py is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# otp-updater.py is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with otp-updater.py; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# This program uses some code borrowed from GTFS manager by geOps
# (https://github.com/geops/gtfsman)

"""

Usage:
  otp-updater.py [--config-file=<path>] [--otp-base-dir=<path>] [--feed-list=<path>] [--otp-command=<path>] [--force-rebuild] [--keep-failed-graphs] [--otp-log-path=<path>] [--only-process-graph=<graph>]

Options:
  -h --help              Show this screen.
  --version              Show version.
  --config-file=<path>   Path to config file (default: /etc/otp-updater/otp-updater.conf)
  --otp-base-dir=<path>  Base directory for OpenTripPlanner's data (default: /var/otp).
  --feed-list=<path>     CSV list of feeds (default: /etc/otp-updater/gtfs-feeds.conf).
  --otp-command=<path>   Full path to the OpenTripPlanner launcher
                         (used to trigger rebuilds of graphs)
  --force-rebuild        Always trigger rebuild of all graphs (used mainly
                         for debugging)
  --keep-failed-graphs   Don't clean graph directories that failed to be built
                         successfully
  --otp-log-path=<path>  Path to store output from the "otp --build" command
                         runs in (default: current working directory)
  --only-process-graph=<graph> Only process the specified graph from the feed list,
                         this can be used to bootstrap a new feed when added for example
"""

import csv
import os
import urllib.request, urllib.error, urllib.parse
import http.client
import tempfile
import shutil
import hashlib
import configparser
from urllib.parse import urlparse
from dateutil import parser
from datetime import datetime
from subprocess import call

class GTFSUpdater(object):

    def __init__(self, options):
        self.options = options
        self._updated_graphs = set()

        self.read_config()

        if not self.options['--otp-base-dir']: self.options['--otp-base-dir'] = "/var/otp"
        if not self.options['--feed-list']: self.options['--feed-list'] = "/etc/gtfs-feeds.conf"

    def read_config(self):
        optional_config = self.options['--config-file']
        path = optional_config if optional_config else '/etc/otp-updater/otp-updater.conf'
        config = configparser.ConfigParser()

        config.read(path)

        otp_options = config['OpenTripPlanner']
        updater_options = config['Updater options']
        gtfs_options = config['GTFS feeds']
        
        if not self.options['--otp-base-dir'] and 'otp_base_dir' in otp_options:
            self.options['--otp-base-dir'] = otp_options['otp_base_dir']

        if not self.options['--otp-command'] and 'otp_command' in otp_options:
            self.options['--otp-command'] = otp_options['otp_command']

        if not self.options['--feed-list'] and 'feed_list' in gtfs_options:
            self.options['--feed-list'] = gtfs_options['feed_list']

        if not self.options['--force-rebuild'] and 'force_rebuild' in updater_options:
            force_rebuild = updater_options['force_rebuild']
            if force_rebuild == 'true':
                self.options['--force-rebuild'] = True

        if not self.options['--keep-failed-graphs'] and 'keep_failed_graphs' in updater_options:
            keep_failed_graphs = updater_options['keep_failed_graphs']
            if keep_failed_graphs == 'true':
                self.options['--keep-failed-graphs']

        if not self.options['--otp-log-path'] and 'log_path' in updater_options:
            log_path = updater_options['log_path']
            self.options['--otp-log-path'] = log_path if log_path else ''

    def update_feeds(self):
        with open(self.options['--feed-list'], 'r') as feed_list:
            reader = iter(csv.reader(feed_list))

            for row in reader:
                # skip empty lines
                if len(row) == 0:
                    continue
                # skip lines starting with a #
                if row[0].startswith('#'):
                    continue
                if len(row) < 3 or len(row) > 4:
                    print("Incorrect feed spec " + str(row))
                    self._found_error = True
                    continue
                self._update_feed(row)
                # output empty line
                print('')
        # update graphs
        self._update_graphs()

    def found_error(self):
        return self._found_error

    def _is_force_rebuild_set(self):
        return self.options['--force-rebuild']

    def _get_otp_base_dir(self):
        return self.options['--otp-base-dir']

    def _is_keep_failed_graphs_set(self):
        return self.options['--keep-failed-graphs']

    def _get_otp_log_path(self):
        log_path = self.options['--otp-log-path']
        return log_path if log_path else ''
    
    def _update_feed(self, row):
        otp_base_dir = self._get_otp_base_dir()
        graph = row[0]
        feed = row[1]
        feed_url = row[2]

        # check if we process this feed
        only_process_graph = self.options['--only-process-graph']

        if only_process_graph and only_process_graph != graph:
            return
        
        self._create_graph_dir(graph)
        
        print("Processing feed: " + feed + ", for graph: " + graph)

        # if the force rebuild option is set, unconditionally add to graphs
        # to be updated
        if self._is_force_rebuild_set():
            print('The force rebuild option was set, so unconditionally add graph to be rebuilt')
            self._updated_graphs.add(graph)

        
        # if there is a feed_info.txt URL, check if there is a new version
        if len(row) == 4:
            feed_info_url = row[3]
            stored_feed_info_path = os.path.join(otp_base_dir, 'graphs', graph, feed + '_feed_info.txt')
            
            print("Checking feed info: " + feed_info_url)
            
            fetched_feed_info = self._fetch_file(feed_info_url)

            if fetched_feed_info != None:
                if os.path.exists(stored_feed_info_path):
                    with open(stored_feed_info_path, 'rb') as stored_feed_info:
                        # compare fetched feed info with the stored one
                        if self._is_files_identical(fetched_feed_info, stored_feed_info):
                            print('Feed info is not updated, skipping')
                            return

                # store fetched feed_info.txt                
                shutil.copyfile(fetched_feed_info.name, stored_feed_info_path)
        # try to see when feed was updated on the server
        local_feed_path = os.path.join(otp_base_dir, 'graphs', graph, feed + '.zip')
        u = urlparse(feed_url)
        if u.scheme == 'https' or u.scheme == 'http':
            remote_feed_updated = self._get_last_modified_for_url(feed_url)

            if os.path.exists(local_feed_path):
                local_feed_updated = datetime.fromtimestamp(os.path.getmtime(local_feed_path))
                
                print('Remote feed updated on: ' + str(remote_feed_updated))
                print('Local feed updated on: ' + str(local_feed_updated))
                
                if remote_feed_updated != None and remote_feed_updated <= local_feed_updated:
                    print('Local feed is up-to-date, skipping')
                    return

        if u.scheme == 'file':
            print('Reading locally supplied feed from: ' + u.path)
            new_feed = open(u.path, 'rb')
        else:
            print('Downloading GTFS feed from: ' + feed)
            new_feed = self._fetch_file(feed_url)

        if new_feed != None:
            # check if the GTFS file was really updated
            if os.path.exists(local_feed_path):
                with open(local_feed_path, 'rb') as local_feed:
                    if not self._is_files_identical(local_feed, new_feed):
                        # copy in new GTFS feed
                        print('GTFS file has been updated, replace with new')
                        shutil.copyfile(new_feed.name, local_feed_path)
                        # add to graphs to update
                        self._updated_graphs.add(graph)
            else:
                # if the GTFS feed weren't already present, copy it in and
                # trigger a graph build
                print('Adding new GTFS feed')
                shutil.copyfile(new_feed.name, local_feed_path)
                self._updated_graphs.add(graph)
                
    def _update_graphs(self):
        print('Rebuilding updated graphs')
        for graph in self._updated_graphs:
            self._update_graph(graph)

    def _update_graph(self, graph):
        command = self.options['--otp-command']
        graph_path = os.path.join(self._get_otp_base_dir(), 'graphs', graph)
        otp_log_path = os.path.join(self._get_otp_log_path(), 'otp-build-' + graph + '.log')

        print('Running OTP command: ' + command)
        print('with path: ' + graph_path)

        with open(otp_log_path, 'w') as outfile:
            retcode = call([command, '--build', graph_path], stdout=outfile, stderr=outfile)

        if retcode == 0:
            print('Sucessfully updated graph')
        else:
            print('Error updating graph')
            self._found_error = True
            if not self._is_keep_failed_graphs_set():
                print('Deleteing failed graph directory')
                self._delete_graph_dir(graph_path)

    # create graph dir if it doesn't exist
    def _create_graph_dir(self, graph):
        path = os.path.join(self._get_otp_base_dir(), 'graphs', graph)
        if not os.path.exists(path):
            print("Graph dir " + path + " didn't exist, so creating it")
            os.makedirs(path)

    def _delete_graph_dir(self, graph):
        path = os.path.join(self._get_otp_base_dir(), 'graphs', graph)
        if os.path.exists(path):
            shutil.rmtree(path)

    def _fetch_file(self, url):
        output = tempfile.NamedTemporaryFile()

        try:
            response = urllib.request.urlopen(url)
            u = urlparse(url)
            
            if u.scheme == 'ftp' or response.getcode() == 200:
                block_sz = 8192
                while True:
                    buffer = response.read(block_sz)
                    if not buffer:
                        break
                    output.write(buffer)
                    
                output.seek(0)
                output.flush()
                print('Wrote output to temporary file: ' + output.name)
                return output
            else:
                print('Error fetching URL: ' + url + ': ' + response.message)
                self._found_error = True
                return None
        except:
            print('Error opening URL: ' + url)
            self._found_error = True
            return None

    def _get_last_modified_for_url(self, url):
        u = urlparse(url)
        if u.scheme == 'https':
            conn = http.client.HTTPSConnection(u.netloc)
        else:
            conn = http.client.HTTPConnection(u.netloc)

        if len(u.query) > 0:
            conn.request('HEAD', u.path + '?' + u.query)
        else:
            conn.request('HEAD', u.path)
        res = conn.getresponse()

        if res.status == 200:
            mod = dict(res.getheaders()).get('last-modified', None)
            if not mod:
                return None
            else:
                return parser.parse(mod, ignoretz=True)
        else:
             print('Failed to get last-modified from server')
             return None

        
    def _is_files_identical(self, file1, file2):
        hash1 = self._sha256hash(file1)
        hash2 = self._sha256hash(file2)
        return hash1 == hash2
            
    def _sha256hash(self, file):
        blocksize = 65536
        hasher = hashlib.sha256()
        buf = file.read(blocksize)
        while len(buf) > 0:
            hasher.update(buf)
            buf = file.read(blocksize)
        return hasher.hexdigest()
    
def main(options=None):
    updater = GTFSUpdater(options)
    updater.update_feeds()
    return 255 if updater.found_error else 0
    
if __name__ == '__main__':
    from docopt import docopt

    arguments = docopt(__doc__, version='otp-updater 0.0')
    try:
        retcode = main(options=arguments)
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        retcode = 1
    exit(retcode)
