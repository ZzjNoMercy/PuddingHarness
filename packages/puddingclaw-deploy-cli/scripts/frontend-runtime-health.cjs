// Installed only into the independently built standalone runtime. The public
// identity is not the private launcher-control token.
const http = require('node:http');
const originalCreateServer = http.createServer;
http.createServer = function (...args) {
  const server = originalCreateServer.apply(this, args);
  const originalEmit = server.emit;
  server.emit = function (event, ...values) {
    if (event === 'request') {
      const [request, response] = values;
      if (request.method === 'GET' && request.url === '/.puddingharness/health') {
        const instanceId = process.env.PUDDINGHARNESS_INSTANCE_ID;
        response.writeHead(instanceId ? 200 : 503, {'Content-Type':'application/json','Cache-Control':'no-store'});
        response.end(JSON.stringify({name:'PuddingHarness',role:'frontend',instance_id:instanceId || null}));
        return true;
      }
    }
    return originalEmit.call(this, event, ...values);
  };
  return server;
};
