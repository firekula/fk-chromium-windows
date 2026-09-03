import assert from 'node:assert/strict';
import test from 'node:test';

import {
    BUILD_IDENTITY_FIELDS,
    assertMatchingBuildIdentity,
    uploadArtifactWithRetries,
} from './artifact_contract.js';


const BUILD_IDENTITY = Object.freeze({
    windows_commit: '1'.repeat(40),
    upstream_windows_commit: '2'.repeat(40),
    upstream_commit: '3'.repeat(40),
    branding_commit: '4'.repeat(40),
    upstream_tag: '151.0.7922.173-2.1',
    upstream_version: '151.0.7922.173',
    fk_revision: 1,
    force_rebuild: false,
    publish: false,
    release_tag: '151.0.7922.173-fk.1',
});


test('resume identity requires every source and requested-input field', () => {
    assert.deepEqual(BUILD_IDENTITY_FIELDS, Object.keys(BUILD_IDENTITY));
    assert.doesNotThrow(() => assertMatchingBuildIdentity(BUILD_IDENTITY, {...BUILD_IDENTITY}));

    for (const field of BUILD_IDENTITY_FIELDS) {
        const resumed = {...BUILD_IDENTITY, [field]: `wrong-${field}`};
        assert.throws(
            () => assertMatchingBuildIdentity(BUILD_IDENTITY, resumed),
            new RegExp(`Resume artifact build identity mismatch: ${field}`),
        );
        const missing = {...BUILD_IDENTITY};
        delete missing[field];
        assert.throws(
            () => assertMatchingBuildIdentity(BUILD_IDENTITY, missing),
            new RegExp(`Resume artifact build identity mismatch: ${field}`),
        );
    }
});


test('artifact upload throws after the bounded retry budget is exhausted', async () => {
    let uploads = 0;
    const artifact = {
        async deleteArtifact() {},
        async uploadArtifact() {
            uploads += 1;
            throw new Error(`upload-${uploads}`);
        },
    };

    await assert.rejects(
        uploadArtifactWithRetries(
            artifact,
            'build-artifact',
            ['artifact.zip'],
            'build',
            {retentionDays: 4},
            async () => {},
            () => {},
        ),
        /Failed to upload artifact build-artifact after 5 attempts: upload-5/,
    );
    assert.equal(uploads, 5);
});


test('artifact upload returns immediately after a successful retry', async () => {
    let uploads = 0;
    const deletedNames = [];
    const uploadArguments = [];
    const artifact = {
        async deleteArtifact(name) {
            deletedNames.push(name);
            throw new Error('not found');
        },
        async uploadArtifact(...args) {
            uploadArguments.push(args);
            uploads += 1;
            if (uploads < 3) {
                throw new Error('transient');
            }
            return {id: 42};
        },
    };

    const result = await uploadArtifactWithRetries(
        artifact,
        'fk-chromium-windows-x64',
        ['installer.exe'],
        'build',
        {retentionDays: 4},
        async () => {},
        () => {},
    );

    assert.deepEqual(result, {id: 42});
    assert.equal(uploads, 3);
    assert.deepEqual(deletedNames, Array(3).fill('fk-chromium-windows-x64'));
    assert.deepEqual(
        uploadArguments,
        Array(3).fill([
            'fk-chromium-windows-x64',
            ['installer.exe'],
            'build',
            {retentionDays: 4},
        ]),
    );
});
