import logging
from pathlib import Path
from debian.deb822 import (
    Dsc,
)

from datalad.distribution.dataset import (
    EnsureDataset,
    datasetmethod,
    require_dataset,
)
from datalad.interface.base import (
    Interface,
    build_doc,
)
from datalad.interface.results import get_status_dict
from datalad.interface.utils import (
    eval_results,
)
from datalad.support.constraints import (
    EnsureNone,
    EnsureStr,
)
from datalad.support.param import Parameter

from datalad_debian.utils import result_matches


lgr = logging.getLogger('datalad.debian.new_distribution')


ckwa = dict(
    result_xfm=None,
    result_renderer='disabled',
    return_type='generator',
    # we leave the flow-control to the caller
    on_failure='ignore',
)


@build_doc
class UpdateRepreproRepository(Interface):
    """Update a (reprepro) Debian archive repository dataset
    """
    _params_ = dict(
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""specify a dataset to update""",
            constraints=EnsureDataset() | EnsureNone()),
        path=Parameter(
            args=("path",),
            nargs='?',
            metavar='PATH',
            doc="""path to constrain the update to""",
            # put dataset 2nd to avoid useless conversion
            constraints=EnsureStr() | EnsureDataset() | EnsureNone()),
    )

    _examples_ = []

    @staticmethod
    @datasetmethod(name='deb_update_reprepro_repository')
    @eval_results
    def __call__(path=None, *, dataset=None):
        reprepro_ds = require_dataset(dataset)

        # TODO allow user-provided reference commitish
        # last recorded update of www subdataset
        last_update_hexsha = reprepro_ds.repo.call_git_oneline(
            ['log', '-1', '--format=%H'], files='www')
        lgr.debug('Using archive update ref %r', last_update_hexsha)

        dist_subdataset_relpath = reprepro_ds.subdatasets(
            'distributions',
            result_xfm='relpaths',
            result_renderer="disabled")

        updated_dists = [
            d for d in reprepro_ds.diff(
                'distributions',
                fr=last_update_hexsha,
                result_xfm='relpaths',
                result_renderer='disabled')
            if d in dist_subdataset_relpath
        ]
        # TODO could be done in parallel
        for ud in updated_dists:
            yield from _get_updates_from_dist(
                reprepro_ds,
                Path(ud),
                last_update_hexsha,
            )


def _get_updates_from_dist(ds, dpath, ref):
    # TODO option to drop distributions that were not present locally before?
    # make sure the distribution dataset is present locally
    lgr.debug('Updating from %s', dpath)
    dist_ds = ds.get(
        path=dpath,
        get_data=False,
        result_xfm='datasets',
        result_renderer='disabled',
        return_type='item-or-list',
    )
    updated_pkg_datasets = [
        # we must use `ds` again to keep the validity of `ref`
        pkg_ds for pkg_ds in ds.diff(
            fr=ref,
            path=dpath / 'packages',
            recursive=True,
            recursion_limit=1,
            result_xfm='datasets',
            result_renderer='disabled',
        )
        # we are not interested in the distribution dataset here
        if pkg_ds != dist_ds
    ]
    # TODO could be done in parallel
    for up in updated_pkg_datasets:
        yield from _get_updates_from_pkg(
            ds,
            dist_ds.pathobj.name,
            up,
            ref,
        )


def _get_updates_from_pkg(ds, dist_codename, pkg_ds, ref):
    # TODO option to drop packages that were not present locally before?
    # make sure the package dataset is present locally
    lgr.debug('Updating from %s', pkg_ds.pathobj.relative_to(ds.pathobj))
    ds.get(
        path=pkg_ds.pathobj,
        get_data=False,
        result_renderer='disabled',
        return_type='item-or-list',
    )
    updated_files = [
        # we must use `ds` again to keep the validity of `ref`
        Path(r['path']) for r in ds.diff(
            fr=ref,
            path=pkg_ds.pathobj,
            recursive=True,
            recursion_limit=2,
            result_renderer='disabled',
        )
        # we can handle three types of files
        # - changes files from builds of any kind
        # - dsc of source packages
        # - lonely debs
        if r.get('state') in ('added', 'modified')
        and Path(r['path']).suffix in ('.changes', '.dsc', '.deb')
    ]
    if not updated_files:
        return
    # TODO option to give a single commit across all updates?
    # it won't have the prov-records from run, but it may be needed
    # for bring the amount of commits down to a sane level for
    # huge archives
    yield from _include_changes(ds, dist_codename, updated_files)
    if not updated_files:
        return
    yield from _include_dsc(ds, dist_codename, updated_files)
    if not updated_files:
        return
    yield from _include_deb(ds, dist_codename, updated_files)


def _include_changes(ds, dist_codename, updated_files):
    for changes in (c for c in updated_files if c.suffix == '.changes'):
        yield changes
    pass


def _include_dsc(ds, dist_codename, updated_files):
    for dsc in (c for c in updated_files if c.suffix == '.dsc'):
        lgr.debug('Import DSC from %s', dsc.relative_to(ds.pathobj))
        ds.get(
            path=dsc,
            get_data=True,
            result_renderer='disabled',
        )
        # pull all files referenced by the DSC from the list of
        # updated files, and do it not to avoid importing
        # pieces in case the dsc import fails for whatever reason
        dsc_files = [
            dsc.parent / f['name']
            for f in Dsc((ds.pathobj / dsc).read_text())['Files']
        ]
        dsc_files.append(dsc)
        for pull_file in dsc_files:
            try:
                updated_files.remove(pull_file)
            except ValueError:
                # file not present, nothing to worry about
                pass
        # TODO add commit message
        yield from ds.run(
            f'reprepro includedsc {dist_codename} {dsc}',
            inputs=[str(p) for p in dsc_files],
            result_renderer='disabled',
        )
        yield get_status_dict(
            status='ok',
            ds=ds,
            action='update_repository.includedsc',
            dsc=str(dsc),
        )


def _include_deb(ds, dist_codename, updated_files):
    for deb in (c for c in updated_files if c.suffix == '.deb'):
        lgr.debug('Import DEB from %s', deb.relative_to(ds.pathobj))
        # TODO add commit message
        yield from ds.run(
            f'reprepro includedeb {dist_codename} {deb}',
            inputs=[str(deb)],
            result_renderer='disabled',
        )
        yield get_status_dict(
            status='ok',
            ds=ds,
            action='update_repository.includedeb',
            deb=str(deb),
        )
