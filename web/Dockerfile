# Build the SPA, then serve the static output from nginx. The runtime image
# contains no Node and no source, only the compiled bundle.
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json ./
# `npm install` rather than `npm ci` against a committed lockfile: npm prunes
# platform-specific optional dependencies (rollup's native binary) from the
# lockfile, so a lockfile generated on one OS/libc fails to build on another.
# Direct dependencies are pinned to exact versions in package.json instead.
RUN --mount=type=cache,target=/root/.npm \
    npm install --no-audit --no-fund

COPY . .
RUN npm run build


FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
