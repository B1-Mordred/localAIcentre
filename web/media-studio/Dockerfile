FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS build

WORKDIR /app
ARG VITE_B1_API_BASE=https://api.ai.b1.germering
ENV VITE_B1_API_BASE=${VITE_B1_API_BASE}
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.29.1-alpine@sha256:42a516af16b852e33b7682d5ef8acbd5d13fe08fecadc7ed98605ba5e3b26ab8
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
