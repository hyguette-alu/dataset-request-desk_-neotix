# Build the SPA, then serve the static output from nginx. The runtime image
# contains no Node and no source, only the compiled bundle.
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
# `npm ci` when a lockfile is committed (reproducible), `npm install` as a
# fallback so a fresh clone without one still builds.
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build


FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
