// Python owns stdin: EOF closes Vite even after an IDE force-stops its parent.
import {createServer} from 'vite';
import {writeSync, closeSync} from 'node:fs';

const backend = new URL(process.argv[2]);
if (backend.protocol !== 'http:' || backend.hostname !== '127.0.0.1'
    || !backend.port || backend.username || backend.password
    || backend.pathname !== '/' || backend.search || backend.hash) {
  throw new Error('An owned loopback backend URL is required');
}

let server;
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  const deadline = setTimeout(() => process.exit(1), 5000).unref();
  try { await server?.close(); }
  finally { clearTimeout(deadline); process.exit(0); }
}
process.stdin.resume();
process.stdin.on('end', stop);
process.on('SIGTERM', stop);
process.on('SIGINT', stop);

try {
  server = await createServer({
    envDir: false,
    envPrefix: [],
    server: {
      host: '127.0.0.1', port: 5173, strictPort: true, open: false,
      ws: {path: '/__vite_hmr'},
      proxy: {
        '/ws': {target: backend.origin, ws: true, changeOrigin: false},
        '/_desktop': {target: backend.origin, changeOrigin: false},
        '/cinema': {target: backend.origin, changeOrigin: false},
      },
    },
  });
  if (!stopping) {
    await server.listen();
    const readinessFd = Number(process.argv[3]);
    writeSync(readinessFd, 'R');
    closeSync(readinessFd);
    server.printUrls();
  }
} catch (error) {
  console.error('Frontend dev stopped (port 5173 must be free):', error.message);
  await server?.close();
  process.exit(1);
}
