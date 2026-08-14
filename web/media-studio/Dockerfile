FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
ARG VITE_B1_API_BASE=
ARG VITE_B1_PUBLIC_API_BASE=https://api.ai.b1.germering
ENV VITE_B1_API_BASE=${VITE_B1_API_BASE}
ENV VITE_B1_PUBLIC_API_BASE=${VITE_B1_PUBLIC_API_BASE}
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.30.4-alpine@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
