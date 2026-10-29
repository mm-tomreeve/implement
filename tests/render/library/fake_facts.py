#!/usr/bin/python
"""Test-only module: return the given dict as host facts."""
from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec={"facts": {"type": "dict", "required": True}})
    module.exit_json(changed=False, ansible_facts=module.params["facts"])


if __name__ == "__main__":
    main()
