with open(r'c:\Users\rcole\Github Projects\tools\builtin.py', 'r', encoding='utf-8') as f:
    broken_content = f.read().split('\n')

with open(r'c:\Users\rcole\Github Projects\tools\builtin_part1.py', 'r', encoding='utf-8') as f:
    part1_content = f.read().split('\n')

# Find TodoTool in broken_content
idx = -1
for i, line in enumerate(broken_content):
    if 'TodoTool(BaseTool)' in line:
        idx = i
        break

if idx != -1:
    part2_content = broken_content[idx:]
    
    missing_part = [
        '            raw = self.agent_state.llm_caller.call_llm(prompt).strip()',
        '            if raw.startswith("```"):',
        '                lines = raw.split("\\n")',
        '                if lines[0].startswith("```"):',
        '                    lines = lines[1:]',
        '                if lines[-1].startswith("```"):',
        '                    lines = lines[:-1]',
        '                raw = "\\n".join(lines).strip()',
        '            ',
        '            import json',
        '            data = json.loads(raw)',
        '            desc = data.get("description", "")',
        '            prio = data.get("priority", "B2")',
        '            deps = data.get("dependencies", [])',
        '            subs = data.get("subtasks", [])',
        '            ',
        '            task = self.add_task(desc, prio, deps, subs)',
        '            return f"Successfully added task: **{desc}** ({task[\'id\']})."',
        '',
        '        elif command == "reorganize":',
        '            prompt_text = input_data.get("prompt", "")',
        '            return self.reorganize_manifest_via_llm(prompt_text)',
        '',
        '        elif command == "get_manifest":',
        '            import os',
        '            state = self.load_state_json()',
        '            guidelines = self.read_guidelines()',
        '            dev_manifest = self.agent_state._read_file_safe(os.path.join(self.agent_state.MEMORY_DIR, "development_manifest.md"))',
        '            phil_manifest = self.agent_state._read_file_safe(os.path.join(self.agent_state.MEMORY_DIR, "philosophy_manifest.md"))',
        '            return {',
        '                "state": state,',
        '                "guidelines": guidelines,',
        '                "development_manifest": dev_manifest,',
        '                "philosophy_manifest": phil_manifest',
        '            }',
        '',
        '        return f"Unknown command for manifest_manager: {command}"',
        ''
    ]
    
    full_content = part1_content + missing_part + part2_content
    with open(r'c:\Users\rcole\Github Projects\tools\builtin.py', 'w', encoding='utf-8') as f:
        f.write('\n'.join(full_content))
    print('Successfully restored builtin.py!')
else:
    print('TodoTool not found in broken_content')
