// the entry links its own stylesheet (plan §1.9): Vite serves the emitted index.css URL for this import
declare module "*.css?url" {
  const href: string;
  export default href;
}
