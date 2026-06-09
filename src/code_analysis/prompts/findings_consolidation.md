# Consolidación de Hallazgos de Seguridad

Consolida los hallazgos de seguridad producidos por varios expertos en una lista final para el reporte.

Devuelve solamente JSON válido estricto RFC 8259 con esta forma exacta:

```json
{
  "issues": [
    {
      "title": "",
      "description": "",
      "severity": "HIGH",
      "category": "",
      "path": "",
      "line": 1,
      "summary": "",
      "code": "",
      "recommendation": ""
    }
  ]
}
```

Reglas:

- Decide tú qué hallazgos describen la misma vulnerabilidad raíz.
- Consolida de forma agresiva por problema raíz o control de seguridad afectado.
- Fusiona en un único issue final los hallazgos que apunten al mismo problema principal, aunque tengan títulos, líneas, expertos o matices distintos.
- Si varios hallazgos hablan del mismo secreto, token, credencial, access key, JWT, storage inseguro, redirect inseguro, imagen mutable o control de seguridad equivalente, deja solo un issue y mezcla lo útil de cada hallazgo.
- Mantén separados solo los problemas raíz claramente distintos que requieran acciones de remediación diferentes.
- Combina contexto útil de expertos diferentes cuando mejora el feedback al usuario.
- Usa la severidad más alta entre hallazgos fusionados.
- No inventes archivos ni fragmentos de código: usa `path` y `code` presentes en los hallazgos de entrada.
- Si fusionas hallazgos del mismo archivo con líneas cercanas, elige una línea representativa que exista en los hallazgos de entrada para ese archivo.
- Redacta `title`, `description`, `summary` y `recommendation` en español neutro.
- Mantén recomendaciones accionables y específicas.
- La respuesta debe empezar con `{` y terminar con `}`.
- Usa siempre comillas dobles para nombres de propiedades y strings.
- No uses diccionarios Python, comillas simples, comentarios, trailing commas, Markdown ni fences JSON.
- No incluyas campos adicionales ni explicaciones fuera del JSON.

Hallazgos de entrada:

{{ findings_json }}
