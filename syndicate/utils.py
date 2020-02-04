import frontmatter
import functools
import github3
import json
import os
import requests

def action_log(msg):
    """(SIDE-EFFECT) Prints `msg` to the Github workflow log."""
    print(msg)

def action_debug(msg):
    """(SIDE-EFFECT) Prints `msg` to the Github workflow debug log."""
    print(f"::debug::{msg}")

def action_warn(msg):
    """(SIDE-EFFECT) Prints `msg` to the Github workflow warning log."""
    print(f"::warning::{msg}")

def action_error(msg):
    """(SIDE-EFFECT) Prints `msg` to the Github workflow error log."""
    print(f"::error::{msg}")

def action_log_group(title):
    """
    Decorates a function such that all its generated log statements are grouped
    in the Github workflow log under `title`.
    """

    def _decorator(func):
        def _wrapper(*args, **kwargs):
            print(f"::group::{title}")
            result = func(*args, **kwargs)
            print("::endgroup::")
            return result
        return _wrapper
    return _decorator

def action_setenv(key, value):
    """
    (SIDE-EFFECT) Sets an environment variable of the running Github workflow job.
    """
    print(f"::set-env name={key}::{value}")

def action_setoutput(key, value):
    """(SIDE-EFFECT) Sets an output variable of the running Github workflow step."""
    print(f"::set-output name={key}::{value}")

def job_addoutput(results):
    """
    (SIDE-EFFECT) Persist `results` for future steps in the running Github
    workflow job.
    """
    syndicated_posts = job_getoutput()
    syndicated_posts.update(results)
    action_setenv('SYNDICATE_POSTS', json.dumps(syndicated_posts))

def job_getoutput():
    """Returns the persisted results of the running Github workflow job."""
    # Default to an empty dictionary if no results have yet been persisted.
    return json.loads(os.getenv('SYNDICATE_POSTS', '{}'))

# Memoize authentication and repo fetching.
@functools.lru_cache(maxsize=1)
def repo():
    """
    (MEMOIZED) Returns an authenticated reference to a `github3` repository
    object for the repository this Github action is running in.
    @see https://github3.readthedocs.io/en/master/api-reference/repos.html#github3.repos.repo.Repository
    """
    assert os.getenv("GITHUB_TOKEN"), "missing GITHUB_TOKEN"
    assert os.getenv("GITHUB_REPOSITORY"), "missing GITHUB_REPOSITORY"

    gh = github3.login(token=os.getenv("GITHUB_TOKEN"))
    return gh.repository(*os.getenv("GITHUB_REPOSITORY").split('/'))

def parent_sha():
    """
    Returns the git SHA to use as parent for any commits generated by this
    Github workflow step.
    """
    assert os.getenv("GITHUB_SHA"), "missing GITHUB_SHA"
    return os.getenv('SYNDICATE_SHA', os.getenv("GITHUB_SHA"))

def get_trigger_payload():
    """
    Returns a list of dictionaries describing each of the modified files in the
    commit that triggered this Github workflow.
    @see https://github3.readthedocs.io/en/master/api-reference/repos.html#github3.repos.comparison.Comparison.files
    """
    assert os.getenv("GITHUB_SHA"), "missing GITHUB_SHA"
    # NOTE
    # Explicitly using GITHUB_SHA to ensure we always have access to the changed
    # files even if other steps generate commits.
    return repo().commit(os.getenv("GITHUB_SHA")).files

def file_contents(filename):
    """
    Returns the `github3` `Contents` object of the matching `filename` in latest
    known commit to this repo.
    @see https://github3.readthedocs.io/en/master/api-reference/repos.html#github3.repos.contents.Contents
    @see :func:`~syndicate.utils.parent_sha`
    """
    # NOTE
    # Using the latest known commit to ensure we capture any modifications made
    # to the post frontmatter by previous actions.
    return repo().file_contents(filename, parent_sha())

def get_posts(post_dir=os.getenv('SYNDICATE_POST_DIR', 'posts')):
    """
    Returns the latest known :func:`~syndicate.utils.file_contents` of the files
    added and modified in the commit that triggered this Github workflow.
    """
    files = get_trigger_payload()
    assert files, "target commit was empty"

    posts = [file for file in files if file['filename'].startswith(post_dir)]
    return [
        file_contents(post['filename'])
        for post in posts
        if post['status'] != 'deleted'  # ignore deleted files
    ]

def fronted(post):
    """
    Returns the :py:class:`frontmatter.Post` representation of the given
    :func:`~syndicate.utils.file_contents` object.

    If `post` is actually already a `frontmatter.Post`, this is a no-op.
    """
    assert post, "missing post"
    if type(post) == frontmatter.Post:
        return post
    raw_contents = post.decoded.decode('utf-8')
    return frontmatter.loads(raw_contents)

def syndicate_key_for(silo):
    """
    Returns a formatted string used to identify a syndicate ID in post
    frontmatter.
    """
    return f'{silo.lower()}_syndicate_id'

def syndicate_id_for(post, silo):
    """
    Retrieves the appropriate post ID for `silo` from the frontmatter of the
    given `post`; returns None if no relevant ID exists.
    """
    assert post, "missing post"
    assert silo, "missing silo"
    return fronted(post).get(syndicate_key_for(silo))

def mark_syndicated_posts(syndicate_ids_by_path, fronted_posts_by_path):
    """
    Injects the given syndicate IDs for the given posts into their frontmatter
    and commits the updated posts back to this repo.

    If a syndicate ID already exists in a given post, it is left untouched.

    Returns a dictionary which is the response of the commit request.
    """
    assert syndicate_ids_by_path, "missing syndicate IDs"
    assert fronted_posts_by_path, "missing fronted posts"

    updated_fronted_posts_by_path = {}
    silos_included = set()
    for (path, syndicate_ids_by_silo) in syndicate_ids_by_path.items():
        fronted_post = fronted_posts_by_path[path]

        # Format:
        # {
        #     'silo_a_syndicate_id': 42,
        #     'silo_b_syndicate_id': 'abc123',
        #     ...
        # }
        new_syndicate_ids = {}
        for (silo, sid) in syndicate_ids_by_silo.items():
            # Ignore already posts already marked with this silo
            if not syndicate_id_for(fronted_post, silo):
                new_syndicate_ids[syndicate_key_for(silo)] = sid
                silos_included.add(silo)

        # Only add to commit if there're any new IDs to add.
        if not new_syndicate_ids:
            continue

        # Create new fronted post with old frontmatter merged with syndicate IDs.
        updated_post = frontmatter.Post(**dict(fronted_post.to_dict(), **new_syndicate_ids))
        updated_fronted_posts_by_path[path] = updated_post
    return commit_updated_posts(updated_fronted_posts_by_path, silos_included)

def commit_updated_posts(fronted_posts_by_path, silos):
    """
    Returns the response of committing the (presumably changed) given posts to
    the remote GITHUB_REF of this repo by following the recipe outlined here:

        https://developer.github.com/v3/git/

    1. Get the current commit object
    2. Retrieve the tree it points to
    3. Retrieve the content of the blob object that tree has for that
       particular file path
    4. Change the content somehow and post a new blob object with that new
       content, getting a blob SHA back
    5. Post a new tree object with that file path pointer replaced with your
       new blob SHA getting a tree SHA back
    6. Create a new commit object with the current commit SHA as the parent
       and the new tree SHA, getting a commit SHA back
    7. Update the reference of your branch to point to the new commit SHA
    """
    if not fronted_posts_by_path:
        action_log("All good: already marked.")
        return None
    assert os.getenv("GITHUB_TOKEN"), "missing GITHUB_TOKEN"
    assert os.getenv("GITHUB_REPOSITORY"), "missing GITHUB_REPOSITORY"
    assert os.getenv("GITHUB_REF"), "missing GITHUB_REF"

    # Create new blobs in the repo's Git database containing the updated contents of our posts.
    new_blobs_by_path = {
        path:repo().create_blob(frontmatter.dumps(fronted_post), 'utf-8')
        for (path, fronted_post) in fronted_posts_by_path.items()
    }
    parent_sha = parent_sha()
    # Create a new tree with our updated blobs.
    new_tree = repo().create_tree(
        [
            {
                'path': path,
                'mode': '100644', # 'file', @see https://developer.github.com/v3/git/trees/#tree-object
                'type': 'blob',
                'sha':  blob_sha
            }
            for (path, blob_sha) in new_blobs_by_path.items()
        ],
        base_tree=parent_sha
    )

    # Update the parent tree with our new subtree.
    # NOTE The github3 package I'm using apparently doesn't support updating refs -_-
    # Hand-rolling my own using the Github API directly.
    # @see https://developer.github.com/v3/
    new_commit = repo().create_commit(
        f'(syndicate): adding IDs for {silos}',
        new_tree.sha,
        [parent_sha]
    )
    response = requests.put(
        f'https://api.github.com/repos/{os.getenv("GITHUB_REPOSITORY")}/git/{os.getenv("GITHUB_REF")}',
        headers={
            'Authorization': f"token {os.getenv('GITHUB_TOKEN')}",
            'Accept': 'application/vnd.github.v3+json'
        },
        json={'sha': new_commit.sha}
    )
    if response.status_code == requests.codes.ok:
        ## NOTE Need to update the reference SHA for future workflow steps.
        action_setenv('SYNDICATE_SHA', new_commit.sha)
        action_log("Syndicate posts marked.")
        return response.json()
    else:
        action_error(f"Failed to mark syndicated posts: {response.json()}")
        return None
