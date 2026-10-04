const path = require("path");
const HtmlWebpackPlugin = require("html-webpack-plugin");
const CopyWebpackPlugin = require("copy-webpack-plugin");
const devCerts = require("office-addin-dev-certs");

/**
 * Webpack configuration for the Git Walk browser app and Excel taskpane.
 *
 * Two entry points:
 *   1. index   → Main browser upload app (index.html)
 *   2. taskpane → Embedded Office.js side panel (taskpane.html)
 *
 * Dev server runs on HTTPS port 3000 (required by Office.js).
 */
module.exports = async (env, argv) => {
  const isProd = argv.mode === "production";
  const officeAddinUrl = (process.env.OFFICE_ADDIN_URL || "https://localhost:3000").replace(/\/$/, "");
  const apiBaseUrl = (process.env.API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
  const httpsOptions = isProd
    ? undefined
    : await devCerts.getHttpsServerOptions(365, ["127.0.0.1", "localhost"]);

  return {
    entry: {
      index: "./src/index.js",
      taskpane: "./src/taskpane.jsx",
    },

    output: {
      path: path.resolve(__dirname, "dist"),
      filename: "[name].[contenthash:8].js",
      clean: true,
      publicPath: "/",
    },

    module: {
      rules: [
        {
          test: /\.jsx?$/,
          exclude: /node_modules/,
          use: {
            loader: "babel-loader",
            options: {
              presets: [
                "@babel/preset-env",
                ["@babel/preset-react", { runtime: "automatic" }],
              ],
            },
          },
        },
        {
          test: /\.css$/,
          use: ["style-loader", "css-loader"],
        },
      ],
    },

    resolve: {
      extensions: [".js", ".jsx"],
    },

    plugins: [
      // Main upload app
      new HtmlWebpackPlugin({
        template: "./public/index.html",
        filename: "index.html",
        chunks: ["index"],
        title: "Git Walk | Spreadsheet version control",
      }),
      // Taskpane app (embedded in Excel)
      new HtmlWebpackPlugin({
        template: "./public/taskpane.html",
        filename: "taskpane.html",
        chunks: ["taskpane"],
        title: "Git Walk for Excel",
      }),
      // Copy manifest.xml to dist
      new CopyWebpackPlugin({
        patterns: [
          {
            from: "public/manifest.xml",
            to: "manifest.xml",
            transform(content) {
              return Buffer.from(content.toString().replaceAll("https://localhost:3000", officeAddinUrl));
            },
          },
        ],
      }),
    ],

    devServer: {
      static: {
        directory: path.resolve(__dirname, "public"),
      },
      port: 3000,
      // Office add-ins require HTTPS. This helper creates, trusts, renews, and
      // returns the same certificate that webpack serves.
      server: {
        type: "https",
        options: httpsOptions,
      },
      hot: true,
      proxy: [
        {
          context: ["/api"],
          target: apiBaseUrl,
          changeOrigin: true,
          secure: false,
        },
      ],
      headers: {
        "Access-Control-Allow-Origin": "*",
      },
      historyApiFallback: {
        rewrites: [
          { from: /^\/taskpane/, to: "/taskpane.html" },
          { from: /./, to: "/index.html" },
        ],
      },
    },

    // "hidden-source-map": still generates .map files for error-tracking
    // tools to consume server-side, but the bundle carries no
    // "//# sourceMappingURL" comment, so a browser (or anyone poking at
    // the deployed dist/ folder) can't pull the original source back out.
    devtool: isProd ? "hidden-source-map" : "eval-source-map",
  };
};
