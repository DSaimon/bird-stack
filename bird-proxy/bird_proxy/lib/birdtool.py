# Copyright (c) 2017 NL-ix
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import errno
import re
import os

from werkzeug.utils import secure_filename

from bird_proxy.lib import bird


class BIRDToolError(Exception):
    pass


class MissingCommandArgument(Exception):
    pass


class BIRDCommand(object):

    ALLOW_EMPTY_LINES = False

    def __init__(self, bird_connection):
        self.bird_connection = bird_connection

    def parse_result(self, result):
        return result

    def execute(self, **kwargs):
        try:
            command = re.sub(' +', ' ', self.COMMAND_TEMPLATE.format(**kwargs))
        except KeyError as e:
            raise MissingCommandArgument("argument {} not specified".format(e))
        result = self.bird_connection.cmd(
            command,
            allow_empty_lines=self.ALLOW_EMPTY_LINES)

        return self.parse_result(result)


class ValidateConfigCommand(BIRDCommand):
    COMMAND_TEMPLATE = 'configure check "{config_filename}"'


class ConfigureCommand(BIRDCommand):
    COMMAND_TEMPLATE = 'configure "{config_filename}"'


class ProtocolInformationCommand(BIRDCommand):
    COMMAND_TEMPLATE = 'show protocols all {wildcard}'

    ALLOW_EMPTY_LINES = True

    def parse_result(self, data):
        success, lines = data

        if not success:
            return success, lines

        results = []
        current_result = None

        for line in lines.splitlines():
            # an empty line indicates the end of the previous result
            if line == '':
                if current_result is not None:
                    results.append(current_result)

                current_result = None
                continue

            new_peer = re.match(r'^\s*(?P<session_name>peer_[^ ]+)', line)
            if new_peer is not None:
                current_result = {
                    'session_name': new_peer.group('session_name'),
                    'description': None,
                    'ip_address': None,
                    'as_number': None,
                    'prefixes': {
                        'imported': 0,
                        'exported': 0,
                        'preferred': 0,
                    },
                }
                continue

            if current_result is None:
                # we cannot continue processing if we didn't find a peer to process
                continue

            description = re.match(r'^\s*Description:\s+(?P<description>.*)$', line)
            if description is not None:
                current_result['description'] = description.group('description')

            routes = re.match(
                r'^\s*Routes:\s+'
                '(?P<imported>\d+) imported, '
                '(?P<exported>\d+) exported, '
                '(?P<preferred>\d+) preferred$', line)
            if routes is not None:
                current_result['prefixes'] = {
                    'imported': int(routes.group('imported')),
                    'exported': int(routes.group('exported')),
                    'preferred': int(routes.group('preferred')),
                }

            bgp_state = re.match(r'^\s*BGP state:\s+(?P<bgp_state>\w+)$', line)
            if bgp_state is not None:
                current_result['bgp_state'] = bgp_state.group('bgp_state')

            ip_address = re.match(
                r'^\s*Neighbor address:\s+'
                '(?P<ip_address>\S+)$', line)
            if ip_address is not None:
                current_result['ip_address'] = ip_address.group('ip_address')

            as_number = re.match(r'^\s*Neighbor AS:\s+(?P<as_number>\d+)$', line)
            if as_number is not None:
                current_result['as_number'] = int(as_number.group('as_number'))

        return True, results


class ShowRouteCommand(BIRDCommand):

    COMMAND_TEMPLATE = 'show route {prefix} {table} {cond} {detail} {export} {protocol}'

    def parse_result(self, data):

        def _format_communities(line):
            return line.replace("(", "").replace(")", "").replace(
                ",", ":").split()

        prefix_regexp = re.compile(r"(?P<prefix>[a-f0-9\.:\/]+)?\s+")

        route_summary_regexp = re.compile(
            r"(?:.*via\s+(?P<peer>[^\s]+) on (?P<interface>[^\s]+)|(?:\w+)?)?\s*"
            r"\[(?P<source>[^\s]+) (?P<date>[^\s]+) (?P<time>[^\]\s]+)(?: from (?P<peer2>[^\s]+))?\]"
        )

        success, lines = data

        if not success:
            return success, lines

        ret = []

        current_prefix = None
        route_data = {}

        for line in lines.splitlines():

            line = line.strip()

            if 'via' in line:
                if route_data:
                    ret.append(route_data)
                    route_data = {}

                prefix_match = prefix_regexp.match(line)
                if prefix_match:
                    current_prefix = prefix_match.group('prefix')

                route_data = {"prefix": current_prefix}
                route_summary_match = route_summary_regexp.match(line)
                route_summary = route_summary_match.groupdict()
                if not route_summary['peer']:
                    route_summary['peer'] = route_summary.pop('peer2')
                else:
                    del route_summary['peer2']
                route_data.update(route_summary)

            if 'BGP.' in line:
                line_body = line[4:]
                line_values = line_body.split(": ")
                key = line_values[0]
                if len(line_values) == 2:
                    value = line_values[1]
                else:
                    value = None
                if key == 'community':
                    value = _format_communities(value)
                route_data.update({key: value})

            if line.startswith('('):
                communities = route_data.get('community', [])
                extra_communities = _format_communities(line)
                route_data.update({'community': communities + extra_communities})
            else:
                continue

        if route_data and route_data not in ret:
            ret.append(route_data)

        return True, ret


class BIRDManager(object):

    def __init__(self, ip_version, bird_proxy_config):

        if ip_version == 'ipv4':
            self.bird_socket_file = bird_proxy_config["BIRD_SOCKET"]

        elif ip_version == 'ipv6':
            self.bird_socket_file = bird_proxy_config["BIRD6_SOCKET"]

        else:
            raise BIRDToolError("Invalid IP version: {}".format(ip_version))

        self.ip_version = ip_version
        self.base_config_folder = bird_proxy_config.get('BIRD_CONFIG_FOLDER')
        self.bird_socket_timeout = bird_proxy_config.get('BIRD_SOCKET_TIMEOUT')

    def connect(self):
        return bird.BirdSocket(
            file=self.bird_socket_file,
            timeout=self.bird_socket_timeout)

    def store_config_file(self, bird_config_file):
        bird_config_filename = os.path.join(
            self.base_config_folder,
            secure_filename(bird_config_file.filename))

        try:
            bird_config_file.save(bird_config_filename)
        except IOError as e:
            raise BIRDToolError("Unable to save config file: {}".format(e))

        return bird_config_filename

    def deploy_config(self, bird_config_file):

        bird_config_filename = self.store_config_file(bird_config_file)
        conn = self.connect()
        validation_out = ValidateConfigCommand(conn).execute(
            config_filename=bird_config_filename)
        if validation_out[0] is not True:
            os.remove(bird_config_filename)
            return validation_out

        configure_out = ConfigureCommand(conn).execute(
            config_filename=bird_config_filename)

        # create symlink to the latest config file to keep track of what file
        # ought to be used as the current bird configuration file
        # note that this step must only take place if configuration went
        # succesfully
        if configure_out[0] is True:
            self.symlink_latest_config_file(bird_config_filename)

        return configure_out

    def symlink_latest_config_file(self, bird_config_filename):
        latest_filename = 'bird-{}-latest.conf'.format(self.ip_version)
        latest_bird_config_path = os.path.join(self.base_config_folder,
                                               latest_filename)

        try:
            os.symlink(bird_config_filename, latest_bird_config_path)

        except OSError as e:
            if e.errno == errno.EEXIST:
                # the symlink already exists; remove it before creating it
                # (os.symlink does not support force)
                os.remove(latest_bird_config_path)
                os.symlink(bird_config_filename, latest_bird_config_path)

            else:
                raise

    def get_protocol_information_verbose(self, wildcard=None):
        conn = self.connect()

        if wildcard is None:
            wildcard = ''

        command = ProtocolInformationCommand(conn)
        result = command.execute(wildcard=wildcard)

        return result

    def get_routes_information(
            self,
            forwarding_table=False, prefix=None,
            table=None,
            fltr=None, where=None,
            detail=False,
            export_mode=None, export_protocol=None,
            protocol=None):

        conn = self.connect()

        if prefix and forwarding_table:
            prefix = "for {}".format(prefix)
        elif not prefix:
            prefix = ""

        if not table:
            table = ""

        if fltr and where:
            raise BIRDToolError("Incoerent show route parameters: fltr, where")
        elif fltr and not where:
            cond = "filter {}".format(fltr)
        elif where and not fltr:
            cond = "where {}".format(where)
        else:
            cond = ""

        if detail is True:
            detail = "all"
        else:
            detail = ""

        if export_mode and export_mode not in ("export", "preexport", "noexport"):
            raise BIRDToolError("Invalid export mode: {}".format(export_mode))
        elif export_mode and not export_protocol:
            raise BIRDToolError("Missing value for export mode")
        elif export_mode and export_protocol:
            export = "{} {}".format(export_mode, export_protocol)
        else:
            export = ""

        if protocol:
            protocol = "protocol {}".format(protocol)
        else:
            protocol = ""

        command = ShowRouteCommand(conn)
        result = command.execute(
            prefix=prefix, table=table,
            cond=cond, detail=detail, export=export,
            protocol=protocol)

        return result
