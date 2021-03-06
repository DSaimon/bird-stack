# Copyright (C) 2017 NL-ix
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

import json


from st2common.runners.base_action import Action
from st2client.client import Client
from st2client.models import KeyValuePair

from lib.birdproxy import BIRDProxyError, RoutesInfoRequest


class GetRoutesInfo(Action):

    def run(self, router_id, ip_version, store_results, ttl=None,
            forwarding_table=False, prefix=None,
            table=None, fltr=None, where=None, detail=False,
            export_mode=None, export_protocol=None, protocol=None):

        try:
            config = self.config['bird_servers'][router_id]
        except KeyError:
            return (False, "Route server {} unknown".format(router_id))

        command_parameters = {
            'forwarding_table': forwarding_table,
            'prefix': prefix,
            'table': table,
            'fltr': fltr,
            'where': where,
            'detail': detail,
            'export_mode': export_mode,
            'export_protocol': protocol
        }

        dest_host = config.get('host')
        listen_web_port = config.get('listen_web_port')
        api_token = config.get('api_token')

        try:
            resp = RoutesInfoRequest(dest_host,
                                     listen_web_port,
                                     api_token,
                                     ip_version,
                                     command_parameters).execute()
        except BIRDProxyError as e:
            return (False, str(e))

        response = resp.get("outcome")
        data = resp.get("message")

        if response and store_results:
            api_base_url = self.config.get('st2_base_url')
            st_client = Client(base_url=api_base_url)
            key = "routes_info_{}_{}".format(
                router_id, ip_version)
            value = json.dumps(data)
            pair = KeyValuePair(name=key,
                                value=value)
            if ttl is not None:
                pair.ttl = ttl
            try:
                st_client.keys.update(pair)
            except Exception as e:
                return (False, str(e))

            return(response, key)
        else:
            return (response, data)
