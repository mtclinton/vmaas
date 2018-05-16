"""
Module to handle /updates API calls.
"""

from jsonschema import validate
from utils import join_packagename, split_packagename

SECURITY_ERRATA_TYPE = 'security'

JSON_SCHEMA = {
    'type' : 'object',
    'required': ['package_list'],
    'properties' : {
        'package_list': {
            'type': 'array', 'items': {'type': 'string'}, 'minItems' : 1
            },
        'repository_list': {
            'type': 'array', 'items': {'type' : 'string'}
            },
        'releasever' : {'type' : 'string'},
        'basearch' : {'type' : 'string'}
    }
}


class UpdatesCache(object):
    """Cache which hold updates mappings."""
    # pylint: disable=too-few-public-methods
    def __init__(self, cursor):
        self.cursor = cursor
        self.evr2id_dict = {}
        self.id2evr_dict = {}
        self.arch2id_dict = {}
        self.id2arch_dict = {}
        self.id2erratatype_dict = {}
        self.packagename2id_dict = {}
        self.id2packagename_dict = {}
        self.arch_compat = {}

        self.prepare()

    def prepare(self):
        """ Read ahead table of keys. """
        # Select all evrs and put them into dictionary
        self.cursor.execute("SELECT id, epoch, version, release from evr")
        for evr_id, evr_epoch, evr_ver, evr_rel in self.cursor.fetchall():
            key = "%s:%s:%s" % (evr_epoch, evr_ver, evr_rel)
            self.evr2id_dict[key] = evr_id
            self.id2evr_dict[evr_id] = {'epoch': evr_epoch, 'version': evr_ver, 'release': evr_rel}

        # Select all archs and put them into dictionary
        self.cursor.execute("SELECT id, name from arch")
        for arch_id, arch_name in self.cursor.fetchall():
            self.arch2id_dict[arch_name] = arch_id
            self.id2arch_dict[arch_id] = arch_name

        # Select all errata types and put them into dictionary
        self.cursor.execute("SELECT id, name from errata_type")
        for type_id, type_name in self.cursor.fetchall():
            self.id2erratatype_dict[type_id] = type_name

        # Select information about archs compatibility
        self.cursor.execute("SELECT from_arch_id, to_arch_id from arch_compatibility")
        for from_arch_id, to_arch_id in self.cursor.fetchall():
            self.arch_compat.setdefault(from_arch_id, []).append(to_arch_id)

        # Select all package names
        self.cursor.execute("SELECT id, name from package_name")
        for name_id, pkg_name in self.cursor.fetchall():
            self.packagename2id_dict[pkg_name] = name_id
            self.id2packagename_dict[name_id] = pkg_name


class UpdatesAPI(object):
    """ Main /updates API class. """
    # pylint: disable=too-few-public-methods
    def __init__(self, cursor, updatescache, repocache):
        self.cursor = cursor
        self.updatescache = updatescache
        self.repocache = repocache

    def process_list(self, data):
        #pylint: disable=too-many-locals,too-many-statements,too-many-branches
        """
        This method is looking for updates of a package, including name of package to update to,
        associated erratum and repository this erratum is from.

        :param data: input json, must contain package_list to find updates for them

        :returns: json with updates_list as a list of dictionaries
                  {'package': <p_name>, 'erratum': <e_name>, 'repository': <r_label>}
        """
        validate(data, JSON_SCHEMA)

        packages_to_process = data['package_list']
        response = {
            'update_list': {},
        }
        auxiliary_dict = {}
        answer = {}

        if not packages_to_process:
            return response

        # Read list of repositories
        repo_ids = None
        provided_repo_labels = None
        if 'repository_list' in data:
            provided_repo_labels = data['repository_list']

            if provided_repo_labels:
                repo_ids = []
                for label in provided_repo_labels:
                    repo_ids.extend(self.repocache.label2ids(label))
        else:
            repo_ids = self.repocache.all_ids()

        # Filter out repositories of different releasever
        releasever = data.get('releasever', None)
        if releasever is not None:
            repo_ids = [oid for oid in repo_ids
                        if self.repocache.get_by_id(oid)['releasever'] == releasever]

        # Filter out repositories of different basearch
        basearch = data.get('basearch', None)
        if basearch is not None:
            repo_ids = [oid for oid in repo_ids
                        if self.repocache.get_by_id(oid)['basearch'] == basearch]

        # Parse input list of packages and create empty update list (answer) for them
        packages_nameids = []
        packages_evrids = []
        nevra2text = {}

        for pkg in packages_to_process:
            pkg = str(pkg)

            # process all packages form input
            if pkg not in auxiliary_dict:
                pkg_name, pkg_epoch, pkg_ver, pkg_rel, pkg_arch = split_packagename(str(pkg))
                auxiliary_dict[pkg] = {}  # fill auxiliary dictionary with empty data for every package
                answer[pkg] = {}          # fill answer with empty data

                evr_key = "%s:%s:%s" % (pkg_epoch, pkg_ver, pkg_rel)
                if evr_key in self.updatescache.evr2id_dict and pkg_arch in self.updatescache.arch2id_dict and \
                                pkg_name in self.updatescache.packagename2id_dict:
                    pkg_name_id = self.updatescache.packagename2id_dict[pkg_name]
                    packages_nameids.append(pkg_name_id)
                    auxiliary_dict[pkg][pkg_name_id] = []

                    evr_id = self.updatescache.evr2id_dict[evr_key]
                    packages_evrids.append(evr_id)
                    auxiliary_dict[pkg]['name_id'] = pkg_name_id
                    auxiliary_dict[pkg]['evr_id'] = evr_id
                    auxiliary_dict[pkg]['arch_id'] = self.updatescache.arch2id_dict[pkg_arch]
                    auxiliary_dict[pkg]['repo_releasevers'] = []
                    auxiliary_dict[pkg]['product_repo_id'] = []
                    auxiliary_dict[pkg]['pkg_id'] = []
                    auxiliary_dict[pkg]['update_id'] = []

        response['update_list'] = answer

        if releasever is not None:
            response['releasever'] = releasever
        if basearch is not None:
            response['basearch'] = basearch
        if provided_repo_labels is not None:
            response.update({'repository_list': provided_repo_labels})

        if not packages_evrids:
            return response

        # Select all packages with given evrs ids and put them into dictionary
        self.cursor.execute("""select id, name_id, evr_id, arch_id, summary, description
                               from package where evr_id in %s
                            """, [tuple(packages_evrids)])
        packs = self.cursor.fetchall()
        nevra2pkg_id = {}
        for oid, name_id, evr_id, arch_id, summary, description in packs:
            key = "%s:%s:%s" % (name_id, evr_id, arch_id)
            nevra2text[key] = {'summary':summary, 'description':description}
            nevra2pkg_id.setdefault(key, []).append(oid)

        pkg_ids = []
        for pkg in auxiliary_dict.values():
            try:
                key = "%s:%s:%s" % (pkg['name_id'],
                                    pkg['evr_id'],
                                    pkg['arch_id'])
                pkg_ids.extend(nevra2pkg_id[key])
                pkg['pkg_id'].extend(nevra2pkg_id[key])
            except KeyError:
                pass

        if not pkg_ids:
            return response

        # Select all repo_id and add mapping to package id
        self.cursor.execute("select pkg_id, repo_id from pkg_repo where pkg_id in %s;", [tuple(pkg_ids)])
        pack_repo_ids = self.cursor.fetchall()
        pkg_id2repo_id = {}

        for pkg_id, repo_id in pack_repo_ids:
            pkg_id2repo_id.setdefault(pkg_id, []).append(repo_id)

        for pkg in auxiliary_dict.values():
            try:
                for pkg_id in pkg['pkg_id']:
                    pkg['repo_releasevers'].extend(
                        [self.repocache.get_by_id(repo_id)['releasever'] for repo_id in pkg_id2repo_id[pkg_id]])
                    # Find updates in repositories provided by same product
                    product_ids = set([self.repocache.id2productid(repo_id) for repo_id in pkg_id2repo_id[pkg_id]])
                    for product_id in product_ids:
                        pkg['product_repo_id'].extend(self.repocache.productid2ids(product_id))
            except KeyError:
                pass

        self.cursor.execute("select name_id, id from package where name_id in %s;", [tuple(packages_nameids)])
        sql_result = self.cursor.fetchall()
        names2ids = {}
        for name_id, oid in sql_result:
            names2ids.setdefault(name_id, []).append(oid)

        for pkg in auxiliary_dict.values():
            try:
                pkg_name_id = pkg['name_id']
                pkg[pkg_name_id].extend(names2ids[pkg_name_id])
            except KeyError:
                pass

        update_pkg_ids = []

        sql = """SELECT package.id
                   FROM package
                   JOIN evr ON package.evr_id = evr.id
                  WHERE package.id in %s and evr.evr > (select evr from evr where id = %s)"""
        for pkg in auxiliary_dict.values():
            if pkg:
                pkg_name_id = pkg['name_id']
                if pkg_name_id in pkg and pkg[pkg_name_id]:
                    self.cursor.execute(sql, [tuple(pkg[pkg_name_id]),
                                              pkg['evr_id']])

                    for oid in self.cursor.fetchall():
                        pkg['update_id'].append(oid[0])
                        update_pkg_ids.append(oid[0])

        pkg_id2repo_id = {}
        pkg_id2errata_id = {}
        pkg_id2full_name = {}
        pkg_id2arch_id = {}
        all_errata = []

        if update_pkg_ids:
            # Select all info about pkg_id to repo_id for update packages
            self.cursor.execute("select pkg_id, repo_id from pkg_repo where pkg_id in %s;", [tuple(update_pkg_ids)])
            all_pkg_repos = self.cursor.fetchall()
            for pkg_id, repo_id in all_pkg_repos:
                pkg_id2repo_id.setdefault(pkg_id, []).append(repo_id)

            # Select all info about pkg_id to errata_id
            self.cursor.execute("select pkg_id, errata_id from pkg_errata where pkg_id in %s;", [tuple(update_pkg_ids)])
            all_pkg_errata = self.cursor.fetchall()
            for pkg_id, errata_id in all_pkg_errata:
                all_errata.append(errata_id)
                pkg_id2errata_id.setdefault(pkg_id, []).append(errata_id)

            # Select full info about all update packages
            self.cursor.execute("SELECT id, name_id, evr_id, arch_id from package where id in %s;",
                                [tuple(update_pkg_ids)])
            packages = self.cursor.fetchall()

            for oid, name_id, evr_id, arch_id in packages:
                full_rpm_name = join_packagename(self.updatescache.id2packagename_dict[name_id],
                                                 self.updatescache.id2evr_dict[evr_id]['epoch'],
                                                 self.updatescache.id2evr_dict[evr_id]['version'],
                                                 self.updatescache.id2evr_dict[evr_id]['release'],
                                                 self.updatescache.id2arch_dict[arch_id])

                pkg_id2full_name[oid] = full_rpm_name
                pkg_id2arch_id[oid] = arch_id

        if all_errata:
            # Select all info about errata
            self.cursor.execute("SELECT id, name, errata_type_id from errata where id in %s;", [tuple(all_errata)])
            errata = self.cursor.fetchall()
            id2errata_dict = {}
            eid2erratatypeid_dict = {}
            all_errata_id = []
            for oid, name, errata_type_id in errata:
                id2errata_dict[oid] = name
                eid2erratatypeid_dict[oid] = errata_type_id
                all_errata_id.append(oid)

            self.cursor.execute("SELECT errata_id, repo_id from errata_repo where errata_id in %s",
                                [tuple(all_errata_id)])
            sql_result = self.cursor.fetchall()
            errata_id2repo_id = {}
            for errata_id, repo_id in sql_result:
                errata_id2repo_id.setdefault(errata_id, []).append(repo_id)

            self.cursor.execute("SELECT errata_id, cve_id from errata_cve where errata_id in %s",
                                [tuple(all_errata_id)])
            sql_result = self.cursor.fetchall()
            errata_id2cve_id = {}
            for errata_id, cve_id in sql_result:
                errata_id2cve_id.setdefault(errata_id, []).append(cve_id)

        # Fill the result answer with update information
        for pkg in auxiliary_dict:
            # Grab summary/description for this pkg-name
            pkg_name, pkg_epoch, pkg_ver, pkg_rel, pkg_arch = split_packagename(str(pkg))

            if 'update_id' not in auxiliary_dict[pkg]:
                continue

            key = "%s:%s:%s" % (auxiliary_dict[pkg]['name_id'],
                                auxiliary_dict[pkg]['evr_id'],
                                auxiliary_dict[pkg]['arch_id'])
            if key in nevra2text:
                response['update_list'][pkg]['summary'] = nevra2text[key]['summary']
                response['update_list'][pkg]['description'] = nevra2text[key]['description']

            response['update_list'][pkg]['available_updates'] = []

            for upd_pkg_id in auxiliary_dict[pkg]['update_id']:
                if pkg_id2arch_id[upd_pkg_id] not in self.updatescache.arch_compat[auxiliary_dict[pkg]['arch_id']] or \
                                upd_pkg_id not in pkg_id2errata_id or upd_pkg_id not in pkg_id2repo_id:
                    continue

                for r_id in pkg_id2repo_id[upd_pkg_id]:
                    # check if update package is in repo provided by same product and releasever is same
                    # and if the list of repositories for updates is provided, also check repo id in this list
                    if r_id not in auxiliary_dict[pkg]['product_repo_id'] or r_id not in repo_ids or \
                            self.repocache.get_by_id(r_id)['releasever'] not in \
                                    auxiliary_dict[pkg]['repo_releasevers']:
                        continue

                    errata_ids = pkg_id2errata_id[upd_pkg_id]
                    for e_id in errata_ids:
                        # check current erratum is security or has some linked cve
                        # and it is in the same repo with update pkg
                        if (self.updatescache.id2erratatype_dict[eid2erratatypeid_dict[e_id]] == SECURITY_ERRATA_TYPE
                                or e_id in errata_id2cve_id and errata_id2cve_id[e_id]) \
                                and r_id in errata_id2repo_id[e_id]:
                            e_name = id2errata_dict[e_id]
                            r_dict = self.repocache.get_by_id(r_id)

                            response['update_list'][pkg]['available_updates'].append({
                                'package': pkg_id2full_name[upd_pkg_id],
                                'erratum': e_name,
                                'repository': r_dict['label'],
                                'basearch': r_dict['basearch'],
                                'releasever': r_dict['releasever'],
                                })

        return response
