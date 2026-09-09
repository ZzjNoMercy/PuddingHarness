const net = require('net');

function validatePort(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`Invalid service port: ${port}`);
  }
}

function isPortOccupied(port, { host = '127.0.0.1', timeoutMs = 500 } = {}) {
  validatePort(port);
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    let settled = false;
    const finish = (occupied) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(occupied);
    };
    socket.once('connect', () => finish(true));
    socket.once('error', (error) => {
      // ECONNREFUSED is the only positive proof that no listener owns the
      // local port. Unknown errors fail closed and are treated as occupied.
      finish(error.code !== 'ECONNREFUSED');
    });
    socket.setTimeout(timeoutMs, () => finish(true));
  });
}

async function assertPortAvailable(port, service) {
  if (await isPortOccupied(port)) {
    throw new Error(
      `${service} port ${port} is already in use; refusing to terminate or reuse an unmanaged process`,
    );
  }
}

module.exports = { assertPortAvailable, isPortOccupied };
