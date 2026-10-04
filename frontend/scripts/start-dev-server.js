const https = require("https");
const net = require("net");
const path = require("path");
const { spawn } = require("child_process");

const PORT = 3000;
const ADDIN_ID = "e4f5a6b7-c8d9-0e1f-2a3b-4c5d6e7f8a9b";

function portIsListening() {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port: PORT });
    socket.setTimeout(1200);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => resolve(false));
  });
}

function existingServerIsGitWalk() {
  return new Promise((resolve) => {
    const request = https.get(
      {
        hostname: "localhost",
        port: PORT,
        path: "/manifest.xml",
        rejectUnauthorized: false,
        timeout: 2000,
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          resolve(response.statusCode === 200 && body.includes(ADDIN_ID));
        });
      },
    );
    request.once("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.once("error", () => resolve(false));
  });
}

async function start() {
  if (await portIsListening()) {
    if (await existingServerIsGitWalk()) {
      console.log(`Git Walk is already running at https://localhost:${PORT}. Reusing it.`);
      return;
    }
    throw new Error(
      `Port ${PORT} is being used by another application. Stop that application or configure a different Git Walk port.`,
    );
  }

  const webpackCli = path.join(
    __dirname,
    "..",
    "node_modules",
    "webpack-cli",
    "bin",
    "cli.js",
  );
  const child = spawn(
    process.execPath,
    [webpackCli, "serve", "--mode", "development", "--open"],
    { stdio: "inherit", windowsHide: true },
  );

  const stopChild = () => {
    if (!child.killed) child.kill();
  };
  process.once("SIGINT", stopChild);
  process.once("SIGTERM", stopChild);
  child.once("error", (error) => {
    throw error;
  });
  child.once("exit", (code) => {
    process.exitCode = code ?? 1;
  });
}

start().catch((error) => {
  console.error(`Unable to start Git Walk: ${error.message}`);
  process.exitCode = 1;
});
