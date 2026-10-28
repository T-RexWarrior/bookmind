// Legacy deployment overrides enter through this single boundary.
for (const [key, value] of Object.entries(process.env)) {
  if (key.startsWith('DEEPTUTOR_')) {
    const destination = key.replace(/^DEEPTUTOR_/, 'BOOKMIND_');
    if (process.env[destination] === undefined) process.env[destination] = value;
  }
}
