#!/usr/bin/env conda-execute

# conda execute
# env:
#  - python ==3.5
#  - conda-build
#  - conda-smithy
#  - beautifulsoup4
#  - gitpython
#  - jinja2
#  - pygithub >=1.29
#  - pyyaml
#  - requests
#  - setuptools
#  - tqdm
# channels:
#  - conda-forge
# run_with: python

"""
Usage: python tick_my_feedstocks.py [--password <github_password_or_oauth>] [--user <github_username>] [--no-regenerate --no-rerender --dry-run]

NOTE that your oauth token should have these abilities:
* public_repo
* read:org
* delete_repo.

This script:
1 identifies all of the feedstocks maintained by a user
2 attempts to determine F, the subset of feedstocks that need updating
3 attempts to determine F_i, the subset of F that have no dependencies
  on other members of F
4 attempts to patch each member of F_i with the new version number and hash
5 attempts to regenerate each member of F_i with the installed version
  of conda-smithy
6 submits a pull request for each member of F_i to the appropriate
  conda-forge repoository

IMPORTANT NOTES:
* We get version information from PyPI. If the feedstock isn't based on PyPI,
  it will raise an error. (Execution will continue.)
* All feedstocks updated with this script SHOULD BE DOUBLE-CHECKED! Because
  conda-forge tests are lightweight, even if the requirements have changed the
  tests may still pass successfully.
"""

# TODO pass token/user to pygithub for push. (Currently uses system config.)
# TODO Modify --dry-run flag to list which repos need forks.
# TODO Modify --dry-run flag to list which forks are dirty.
# TODO Modify --dry-run to also cover regeneration
# TODO Add support for skipping repos that are deprecated. (e.g. fake-factory)
# TODO Test python 2.7 compatability (should work, but untested.)
# TODO Test python 3.4 compatability (should work, but untested.)
# TODO Test python 3.6 compatability (should work, but untested.)
# TODO Deeper check of dependency changes in meta.yaml.
# TODO Check installed conda-smithy against current feedstock conda-smithy.
# TODO Check special case of feedstocks renamed with 'python-' prefixes
# TODO Check if already-forked feedstocks have open pulls.

import argparse
from base64 import b64encode
from bs4 import BeautifulSoup
from collections import defaultdict
from collections import namedtuple
import conda_smithy
import conda_smithy.configure_feedstock
from git import Actor
from git import Repo
from github import Github
from github import GithubException
from jinja2 import Template
from jinja2 import UndefinedError
import os
from pkg_resources import parse_version
import re
import requests
import tempfile
from tqdm import tqdm
import yaml


pypi_pkg_uri = 'https://pypi.python.org/pypi/{}/json'.format

fs_tuple = namedtuple('fs_tuple', ['success', 'needs_update', 'data'])

status_data = namedtuple('status_data', ['text', 'yaml_strs',
                                         'pypi_version', 'reqs',
                                         'blob_sha'])

fs_status = namedtuple('fs_status', ['fs', 'status'])

patch_tuple = namedtuple('patch_tuple', ['success', 'data'])


def pypi_org_sha(package_name, version, bundle_type):
    """
    Scrape pypi.org for SHA256 of the source bundle
    :param str package_name: Name of package (PROPER case)
    :param str version: version for which to get sha
    :param str bundle_type: ".tar.gz", ".zip" - format of bundle
    :returns: `str` -- SHA256 for a source bundle
    """
    r = requests.get('https://pypi.org/project/{}/{}/#files'.format(
        package_name,
        version))

    bs = BeautifulSoup(r.text, 'html5lib')
    try:
        sha_val = bs.find('a',
                          {'href':
                           re.compile(
                               'https://files.pythonhosted.org.*{}-{}{}'.
                               format(package_name,
                                      version,
                                      bundle_type))
                           }).next.next.next['data-clipboard-text']
    except AttributeError:
        # Bad parsing of page, couldn't get SHA256
        return None

    return sha_val


def pypi_version_str(package_name):
    """
    Retrive the latest version of a package in pypi
    :param str package_name:
    :return: `str` -- Version string
    """
    r = requests.get(pypi_pkg_uri(package_name))
    if not r.ok:
        return False
    return r.json()['info']['version'].strip()


def parsed_meta_yaml(text):
    """
    :param str text: The raw text in conda-forge feedstock meta.yaml file
    :return: `dict|None` -- parsed YAML dict if successful, None if not
    """
    try:
        yaml_dict = yaml.load(Template(text).render(),
                              Loader=yaml.BaseLoader)
    except UndefinedError:
        # assume we hit a RECIPE_DIR reference in the vars and can't parse it.
        # just erase for now
        try:
            yaml_dict = yaml.load(
                Template(re.sub('{{ (environ\[")?RECIPE_DIR("])? }}/',
                                '',
                                text)
                         ).render(),
                Loader=yaml.BaseLoader)
        except:
            return None
    except:
        return None

    return yaml_dict


def basic_patch(text, yaml_strs, pypi_version, blob_sha):
    """
    Given a meta.yaml file, version strings, and appropriate hashes,
    find and replace old versions and hashes, and create a patch.
    :param str text: The raw text of the current meta.yaml
    :param dict yaml_strs: Dict with 'source_fn', 'version', and 'sha256' values parsed from yaml
    :param str pypi_version: The new version string from PyPI
    :param str blob_sha: the commit SHA code.
    :return: `patch_tuple` -- True if success and commit dict for github, false and error otherwise
    """
    pypi_sha = pypi_org_sha(
        '-'.join(yaml_strs['source_fn'].split('-')[:-1]),
        pypi_version,
        yaml_strs['source_fn'].split(yaml_strs['version'])[-1]
    )

    if pypi_sha is None:
        return patch_tuple(False,
                           "Couldn't get SHA from PyPI")

    if text.find(yaml_strs['version']) < 0 or text.find(yaml_strs['sha256']) < 0:
        # if we can't change both the version and the hash
        # do nothing
        return patch_tuple(False,
                           "Couldn't find current version or SHA in meta.yaml")

    new_text = text.replace(yaml_strs['version'], pypi_version).\
        replace(yaml_strs['sha256'], pypi_sha)

    commit_dict = {
        'message': 'Tick version to {}'.format(pypi_version),
        'content': b64encode(new_text.encode('utf-8')).decode('utf-8'),
        'sha': blob_sha
    }

    return patch_tuple(True, commit_dict)


def user_feedstocks(user):
    """
    :param github.AuthenticatedUser.AutheticatedUser user:
    :return: `list` -- list of conda-forge feedstocks the user maintains
    """
    feedstocks = []
    for team in tqdm(user.get_teams(), desc='Finding feedstock teams...'):

        # Each conda-forge team manages one feedstock
        # If a team has more than one repo, skip it.
        if team.repos_count != 1:
            continue

        repo = list(team.get_repos())[0]
        if repo.full_name.startswith('conda-forge/') and \
                repo.full_name.endswith('-feedstock'):
            feedstocks.append(repo)

    return feedstocks


def feedstock_status(feedstock):
    """
    Return whether or not a feedstock is out of date and any information needed to update it.
    :param github.Repository.Repository feedstock:
    :return: `tpl(bool,bool,None|status_data)` -- bools indicating success and either None or a status_data tuple
    """

    meta_yaml = feedstock.get_contents('recipe/meta.yaml')
    text = meta_yaml.decoded_content.decode('utf-8')

    yaml_dict = parsed_meta_yaml(text)
    if yaml_dict is None:
        return fs_tuple(False, False, "Couldn't parse meta.yaml")

    yaml_strs = dict()
    for x, y in [('version', ('package', 'version')),
                 ('source_fn', ('source', 'fn')),
                 ('sha256', ('source', 'sha256'))]:
        try:
            yaml_strs[x] = yaml_dict[y[0]][y[1]]
        except KeyError:
            return fs_tuple(False, False, 'Missing meta.yaml key: [{}][{}]'.format(y[0], y[1]))

    pypi_version = pypi_version_str(feedstock.full_name[12:-10])
    if pypi_version is False:
        return fs_tuple(False, False, "Couldn't find package in PyPI")

    if parse_version(yaml_strs['version']) >= parse_version(pypi_version):
        return fs_tuple(True, False, None)

    reqs = set()
    for step in yaml_dict['requirements']:
        reqs.update({x.split()[0] for x in yaml_dict['requirements'][step]})

    return fs_tuple(True,
                    True,
                    status_data(text,
                                yaml_strs,
                                pypi_version,
                                reqs - {'python', 'setuptools'},
                                meta_yaml.sha))


def get_user_fork(user, feedstock):
    """
    Return a user's fork of a feedstock if one exists, else create a new one.
    :param github.AuthenticatedUser.AuthenticatedUser user:
    :param github.Repository.Repository feedstock: conda-forge feedstock
    :return: `github.Repository.Repository` -- fork of the feedstock beloging to user
    """
    for fork in feedstock.get_forks():
        if fork.owner.login == user.login:
            return fork

    return user.create_fork(feedstock)


def even_feedstock_fork(user, feedstock):
    """
    Return a fork that's even with the latest version of the feedstock
    If the user has a fork that's ahead of the feedstock, do nothing
    :param github.AuthenticatedUser.AuthenticatedUser user: GitHub user
    :param github.Repository.Repository feedstock: conda-forge feedstock
    :return: `None|github.Repository.Repository` -- None if no fork, else the repository
    """
    fork = get_user_fork(user, feedstock)

    comparison = fork.compare(base='{}:master'.format(user.login),
                              head='conda-forge:master')

    if comparison.behind_by > 0:
        # head is *behind* the base
        # conda-forge is behind the fork
        # leave everything alone - don't want a mess.
        return None

    elif comparison.ahead_by > 0:
        # head is *ahead* of base
        # conda-forge is ahead of the fork
        # delete fork and clone from scratch
        try:
            fork.delete()
        except GithubException:
            # couldn't delete feedstock
            # give up, don't want a mess.
            return None

        fork = user.create_fork(feedstock)

    return fork


def regenerate_fork(fork):
    """
    :param github.Repository.Repository fork: fork of conda-forge feedstock
    :return: `bool` -- True if regenerated, false otherwise
    """
    # Would need me to pass gh_user, gh_password
    # subprocess.run(["./renderer.sh", gh_user, gh_password, fork.name])

    working_dir = tempfile.TemporaryDirectory()
    r = Repo.clone_from(fork.clone_url, working_dir.name)
    conda_smithy.configure_feedstock.main(working_dir.name)

    if not r.is_dirty():
        # No changes made during regeneration.
        # Clean up and return
        working_dir.cleanup()
        return False

    commit_msg = 'MNT: Updated the feedstock for conda-smithy version {}.'.format(
        conda_smithy.__version__)
    r.git.add('-A')
    commit = r.index.commit(commit_msg,
                            author=Actor(fork.owner.login, fork.owner.email))
    r.git.push()

    working_dir.cleanup()
    return True


def tick_feedstocks(gh_password=None, gh_user=None, no_regenerate=False, dry_run=False):
    """
    Finds all of the feedstocks a user maintains that can be updated without
    a dependency conflict with other feedstocks the user maintains,
    creates forks, ticks versions and hashes, and regenerates,
    then submits a pull
    :param str|None gh_password: GitHub password or OAuth token (if omitted, check environment vars)
    :param str|None gh_user: GitHub username (can be omitted with OAuth)
    :param bool no_regenerate: If True, don't regenerate feedstocks before submitting pull requests
    :param bool dry_run: If True, do not apply generate patches, fork feedstocks, or regenerate
    """

    if gh_password is None:
        gh_password = os.getenv('GH_TOKEN')
        if gh_password is None:
            raise ValueError('No password or OAuth token provided, '
                             'and no OAuthToken as GH_TOKEN in environment.')

    if gh_user is None:
        g = Github(gh_password)
        user = g.get_user()
        gh_user = user.login
    else:
        g = Github(gh_user, gh_password)
        user = g.get_user()

    feedstocks = user_feedstocks(user)

    can_be_updated = list()
    status_error_dict = defaultdict(list)
    up_to_date_count = 0
    for feedstock in tqdm(feedstocks, desc='Checking feedstock statuses...'):
        status = feedstock_status(feedstock)
        if status.success and status.needs_update:
            can_be_updated.append(fs_status(feedstock, status))
        elif not status.success:
            status_error_dict[status.data].append(feedstock.name)
        else:
            up_to_date_count += 1

    package_names = set([x.fs.name[:-10] for x in can_be_updated])

    indep_updates = [x for x in can_be_updated
                     if len(x.status.data.reqs & package_names) < 1]

    successful_forks = list()
    successful_updates = list()
    patch_error_dict = defaultdict(list)
    error_dict = defaultdict(list)
    for update in tqdm(indep_updates, desc='Updating feedstocks'):
        # generate basic patch
        patch = basic_patch(update.status.data.text,
                            update.status.data.yaml_strs,
                            update.status.data.pypi_version,
                            update.status.data.blob_sha)

        if not patch.success:
            # couldn't apply patch
            patch_error_dict[patch.data].append(update.fs.name)
            continue

        if dry_run:
            # Skip additional processing here.
            continue

        # make fork
        fork = even_feedstock_fork(user, update.fs)
        if fork is None:
            error_dict["Couldn't fork"].append(update.fs.name)
            continue

        # patch fork
        r = requests.put(
            'https://api.github.com/repos/{}/contents/recipe/meta.yaml'.format(
                fork.full_name),
            json=patch.data,
            auth=(gh_user, gh_password))
        if not r.ok:
            error_dict["Couldn't apply patch"].append(update.fs.name)
            continue

        successful_updates.append(update)
        successful_forks.append(fork)

    if no_regenerate:
        print('Skipping regenerating feedstocks.')
    else:
        for fork in tqdm(successful_forks, desc='Regenerating feedstocks...'):
            regenerate_fork(fork)

    pull_count = 0
    for update in tqdm(successful_updates, desc='Submitting pulls...'):
        try:
            update.fs.create_pull(title='Ticked version, '
                                  'regenerated if needed. '
                                  '(Double-check reqs!)',
                                  body='',
                                  head='{}:master'.format(gh_user),
                                  base='master')
        except GithubException:
            continue

        pull_count += 1

    print('{} Total feedstocks checked.')
    print('  {} were up-to-date.'.format(up_to_date_count))
    print('  {} were independent of other out-of-date feedstocks'.format(
        len(indep_updates)))
    print('  {} had pulls submitted.'.format(pull_count))
    print('-----')

    for msg, cur_dict in [("Couldn't check status", status_error_dict),
                          ("Couldn't create patch", patch_error_dict)]:
        if len(cur_dict) > 0:
            print('{}:'.format(msg))
            for error_msg in cur_dict:
                print('  {} ({}):'.format(error_msg,
                                          len(cur_dict[error_msg])))
                for name in cur_dict[error_msg]:
                    print('    {}'.format(name))

    for error_msg in ["Couldn't fork",
                      "Couldn't apply patch",
                      "Couldn't create pull"]:
        if error_msg not in error_dict:
            continue
        print('{} ({}):'.format(error_msg, len(error_dict[error_msg])))
        for name in error_dict[error_msg]:
            print('  {}'.format(name))


def main():
    """
    Parse command-line arguments and run tick_feedstocks()
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--password',
                        default=None,
                        dest='gh_password',
                        help='GitHub password or oauth token')
    parser.add_argument('--user',
                        default=None,
                        dest='gh_user',
                        help='GitHub username')
    parser.add_argument('--no-regenerate',
                        action='store_true',
                        dest='no_regenerate',
                        help="If present, don't regenerate feedstocks "
                        'after updating')
    parser.add_argument('--no-rererender',
                        action='store_true',
                        dest='no_rerender',
                        help="If present, don't regenerate feedstocks "
                        'after updating')
    parser.add_argument('--dry-run',
                        action='store_true',
                        dest='dry_run',
                        help='If present, skip applying patches, forking, '
                        'and regenerating feedstocks')
    args = parser.parse_args()

    tick_feedstocks(args.gh_password,
                    args.gh_user,
                    args.no_regenerate or args.no_rerender,
                    args.dry_run)


if __name__ == "__main__":
    main()
