import * as core from '@actions/core';
import * as io from '@actions/io';
import * as exec from '@actions/exec';
import { DefaultArtifactClient } from '@actions/artifact';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {assertMatchingBuildIdentity, uploadArtifactWithRetries} from './artifact_contract.js';

const BUILD_TIMEOUT_EXIT_CODE = 124;

async function run() {
    process.on('SIGINT', function() {
    })
    const finished = core.getBooleanInput('finished', {required: true});
    const from_artifact = core.getBooleanInput('from_artifact', {required: true});
    console.log(`finished: ${finished}, artifact: ${from_artifact}`);
    if (finished) {
        core.setOutput('finished', true);
        return;
    }

    const artifact = new DefaultArtifactClient();
    const artifactName = 'build-artifact';
    const buildDirectory = 'C:\\ungoogled-chromium-windows\\build';
    const metadataFile = path.join(buildDirectory, 'fk-build-metadata.json');

    if (from_artifact) {
        const artifactInfo = await artifact.getArtifact(artifactName);
        await artifact.downloadArtifact(artifactInfo.artifact.id, {path: buildDirectory});
        const archiveFile = path.join(buildDirectory, 'artifacts.zip');
        const resumeDirectory = 'C:\\ungoogled-chromium-windows\\resume-artifact';
        await io.rmRF(resumeDirectory);
        await exec.exec('7z', ['x', archiveFile, `-o${resumeDirectory}`, '-y']);
        const expectedMetadata = JSON.parse(await fs.readFile(metadataFile, 'utf8'));
        const resumedMetadata = JSON.parse(
            await fs.readFile(path.join(resumeDirectory, 'fk-build-metadata.json'), 'utf8'),
        );
        assertMatchingBuildIdentity(expectedMetadata, resumedMetadata);
        await io.rmRF(path.join(buildDirectory, 'src'));
        await fs.rename(path.join(resumeDirectory, 'src'), path.join(buildDirectory, 'src'));
        await io.rmRF(resumeDirectory);
        await io.rmRF(archiveFile);
    }

    const args = ['build.py', '--ci', '-j', '2']
    await exec.exec('python', ['-m', 'pip', 'install', 'httplib2==0.22.0'], {
        cwd: 'C:\\ungoogled-chromium-windows',
        ignoreReturnCode: true
    });
    const retCode = await exec.exec('python', args, {
        cwd: 'C:\\ungoogled-chromium-windows',
        ignoreReturnCode: true
    });
    if (retCode === 0) {
        let packageOutput = '';
        await exec.exec('python', ['tools\\release_metadata.py', 'package', '--build-dir', 'build'], {
            cwd: 'C:\\ungoogled-chromium-windows',
            listeners: {
                stdout: (data) => {
                    packageOutput += data.toString();
                }
            }
        });
        let packageFiles;
        try {
            const parsed = JSON.parse(packageOutput);
            packageFiles = [parsed.installer, parsed.portable, parsed.checksums];
        } catch (error) {
            throw new Error(`Invalid FK package metadata: ${error}`);
        }
        if (new Set(packageFiles).size !== 3 ||
            !packageFiles.every(file => typeof file === 'string' && file === path.basename(file))) {
            throw new Error('FK package metadata must contain three artifact filenames');
        }
        const packageList = packageFiles.map(file => path.join(buildDirectory, file));
        await Promise.all(packageList.map(file => fs.access(file)));
        await fs.access(metadataFile);
        packageList.push(metadataFile);
        const finalArtifactName = 'fk-chromium-windows-x64';
        await uploadArtifactWithRetries(
            artifact,
            finalArtifactName,
            packageList,
            buildDirectory,
            {retentionDays: 4, compressionLevel: 0},
        );
        core.setOutput('finished', true);
    } else if (retCode === BUILD_TIMEOUT_EXIT_CODE) {
        await new Promise(r => setTimeout(r, 5000));
        await exec.exec('7z', ['a', '-tzip', 'C:\\ungoogled-chromium-windows\\artifacts.zip',
            'src', 'fk-build-metadata.json', '-mx=3', '-mtc=on'], {cwd: buildDirectory});
        await uploadArtifactWithRetries(
            artifact,
            artifactName,
            ['C:\\ungoogled-chromium-windows\\artifacts.zip'],
            'C:\\ungoogled-chromium-windows',
            {retentionDays: 4, compressionLevel: 0},
        );
        core.setOutput('finished', false);
    } else {
        throw new Error(`Build failed with exit code ${retCode}`);
    }
}

run().catch(err => core.setFailed(err.message));
