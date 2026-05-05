/**
 * CLI wrapper: converts a reMarkable .rmdoc zip → PDF
 * Usage: node render.js <input.zip> <output.pdf>
 */

import { readFileSync, writeFileSync } from "fs";
import { basename, extname } from "path";
import { convertDocument } from "./document-converter";

async function main() {
	const [, , inputPath, outputPath] = process.argv;
	if (!inputPath || !outputPath) {
		console.error("Usage: render.js <input.zip> <output.pdf>");
		process.exit(1);
	}

	const zipData = new Uint8Array(readFileSync(inputPath));
	const docId = basename(inputPath, extname(inputPath));

	try {
		const pdfData = await convertDocument(docId, zipData);
		writeFileSync(outputPath, Buffer.from(pdfData));
	} catch (err) {
		console.error(`render error: ${err}`);
		process.exit(1);
	}
}

main();
