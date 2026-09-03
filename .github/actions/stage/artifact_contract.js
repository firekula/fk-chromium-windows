export const BUILD_IDENTITY_FIELDS = Object.freeze([
    'windows_commit',
    'upstream_windows_commit',
    'upstream_commit',
    'branding_commit',
    'upstream_tag',
    'upstream_version',
    'fk_revision',
    'force_rebuild',
    'publish',
    'release_tag',
]);


export function assertMatchingBuildIdentity(expected, resumed) {
    for (const field of BUILD_IDENTITY_FIELDS) {
        if (!Object.hasOwn(expected, field) || !Object.hasOwn(resumed, field) ||
            expected[field] !== resumed[field]) {
            throw new Error(`Resume artifact build identity mismatch: ${field}`);
        }
    }
}


export async function uploadArtifactWithRetries(
    artifact,
    name,
    files,
    rootDirectory,
    options,
    delay = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds)),
    logError = console.error,
) {
    let lastError;
    for (let attempt = 1; attempt <= 5; attempt += 1) {
        try {
            await artifact.deleteArtifact(name);
        } catch (error) {
            // A missing previous artifact is the expected first-upload state.
        }
        try {
            return await artifact.uploadArtifact(name, files, rootDirectory, options);
        } catch (error) {
            lastError = error;
            logError(`Upload artifact ${name} failed on attempt ${attempt}: ${error}`);
            if (attempt < 5) {
                await delay(10000);
            }
        }
    }
    throw new Error(`Failed to upload artifact ${name} after 5 attempts: ${lastError?.message ?? lastError}`);
}
